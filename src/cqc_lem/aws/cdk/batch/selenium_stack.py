from typing import Dict, List, Optional

from aws_cdk import Duration, Stack
from aws_cdk import aws_applicationautoscaling as applicationautoscaling
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk.aws_servicediscovery import DnsRecordType
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class SeleniumStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 hub_memory_limit: int = 512,
                 hub_cpu: int = 256,
                 node_memory_limit: int = 512,
                 node_cpu: int = 256,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create security groups for hub and node communication
        # Add this to your SeleniumStack
        hub_security_group = ec2.SecurityGroup(
            self, "SeleniumHubSecurityGroup",
            vpc=props.ec2_vpc,
            allow_all_outbound=True
        )

        node_security_group = ec2.SecurityGroup(
            self, "SeleniumNodeSecurityGroup",
            vpc=props.ec2_vpc,
            allow_all_outbound=True
        )

        # Allow hub to receive traffic from nodes
        hub_security_group.add_ingress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(props.selenium_hub_port),  # Hub port
            description="Allow node to hub communication"
        )
        hub_security_group.add_ingress_rule(
            peer=node_security_group,
            connection=ec2.Port.tcp(props.selenium_bus_subscribe_port),  # Event bus subscribe
            description="Allow node to hub event bus"
        )
        hub_security_group.add_ingress_rule(
            peer=node_security_group,
            connection=ec2.Port.tcp(props.selenium_bus_publish_port),  # Event bus publish
            description="Allow node to hub event bus"
        )

        # Allow nodes to receive traffic from hub
        node_security_group.add_ingress_rule(
            peer=hub_security_group,
            connection=ec2.Port.tcp_range(props.selenium_node_port, props.selenium_node_port + 345),  # Node ports
            description="Allow hub to node communication"
        )

        # Add the security groups to the props variable
        props.set('sel_hub_sg', hub_security_group)
        props.set('sel_node_sg', node_security_group)

        # Register SeleniumHub resources
        hub_service = self.create_hub_resources(
            identifier="hub",
            props=props,
            memory_limit=hub_memory_limit,
            cpu=hub_cpu)

        # Register Chrome node resources
        node_service = self.create_node_resource(
            identifier="node_chrome",
            props=props,
            image="selenium/node-chrome",
            memory_limit=node_memory_limit,
            cpu=node_cpu)

        # Add the output properties
        self.output_props = props.props.copy()

    @property
    def outputs(self) -> SharedStackProps:
        return SharedStackProps(**self.output_props)

    def create_hub_resources(self,
                             identifier: str,
                             props: SharedStackProps,
                             memory_limit: int = 1024,
                             cpu: int = 512
                             ) -> ecs.FargateService:
        """Create Selenium Hub resources including service, autoscaling, and load balancer configuration

        """
        # Create the ECS Service
        selenium_hub_service = self.create_service(
            identifier=identifier,
            cluster=props.ecs_cluster,
            props=props,
            security_groups=[
                # props.ecs_security_group,
                props.get('sel_hub_sg')
            ],
            task_execution_role=props.task_execution_role,
            environment={
                'GRID_BROWSER_TIMEOUT': '1800',
                # 'GRID_TIMEOUT': '180', # Dont need a grid timeout as auto scale will reduce based on its own metrics
                # 'SE_OPTS': '-debug',
                'SE_VNC_NO_PASSWORD': 'true',
                "SE_NODE_MAX_SESSIONS": str(props.selenium_node_max_sessions),
                "GRID_NEW_SESSION_WAIT_TIMEOUT": "-1",  # Wait indefinitely for node availability
                "GRID_CLOUDWATCH_METRICS": "true"  # Enable CloudWatch metrics

            },
            image=f"selenium/hub:{props.selenium_version}",
            memory_limit=memory_limit,
            cpu=cpu,
            ports=[props.selenium_hub_port, props.selenium_bus_publish_port, props.selenium_bus_subscribe_port],
            log_driver=ecs.LogDriver.aws_logs(
                stream_prefix="selenium_hub_logs",
                log_group=logs.LogGroup.from_log_group_arn(
                    self, "SeleniumHubLogGroup",
                    log_group_arn=props.selenium_log_group_arn
                )
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL",
                            #f"curl -f http://selenium_{identifier}.{props.ecs_default_cloud_map_namespace.namespace_name}:{props.selenium_hub_port}/wd/hub/status || exit 1",
                            f"/opt/bin/check-grid.sh --host 0.0.0.0 --port {props.selenium_hub_port}" # TODO: Should we use the cloud map namespace url for host?
                         ],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(90)
            )
        )

        hub_target_group = elbv2.ApplicationTargetGroup(
            self, f"selenium-{identifier}-target-group",
            target_group_name=f"selenium-{identifier}-target-group",
            vpc=props.ec2_vpc,
            port=props.selenium_hub_port,
            targets=[selenium_hub_service],
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check=elbv2.HealthCheck(
                path="/wd/hub/status",
                port=str(props.selenium_hub_port),
                interval=Duration.seconds(60),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
            deregistration_delay=Duration.seconds(0),  # No deregistration on hub service
            # TODO: Review this deregistration value - does it allow for hub containers to process and auto scale as needed
        )

        elbv2.ApplicationListenerRule(
            self, "SeleniumHubListenerRule",
            listener=props.elbv2_selenium_hub_listener,
            priority=30,
            action=elbv2.ListenerAction.forward(target_groups=[hub_target_group]),
            conditions=[elbv2.ListenerCondition.path_patterns(["*"])],
        )

        # TODO: Remove below - ALB shouldn't listen externally for node subscribe and publish
        '''
        elbv2.ApplicationListenerRule(
            self, "SeleniumBusSubscribeListenerRule",
            listener=props.elbv2_selenium_bus_subscribe_listener,
            priority=31,
            action=elbv2.ListenerAction.forward(target_groups=[hub_target_group]),
            conditions=[elbv2.ListenerCondition.path_patterns(["*"])],
        )

        elbv2.ApplicationListenerRule(
            self, "SeleniumBusPublishListenerRule",
            listener=props.elbv2_selenium_bus_publish_listener,
            priority=32,
            action=elbv2.ListenerAction.forward(target_groups=[hub_target_group]),
            conditions=[elbv2.ListenerCondition.path_patterns(["*"])],
        )
        '''

        # Create autoscaling policy
        hub_scalable_target = self.create_scaling_policy(
            cluster_name=props.ecs_cluster.cluster_name,
            service_name=selenium_hub_service.service_name,
            identifier=identifier,
            min_instances=props.min_instances,
            max_instances=props.max_instances  # Hub typically needs only one instance

        )

        '''
        
        # Parse load balancer name and ID from the token ARN
        lb_name = Fn.select(1, Fn.split('/', Fn.select(1, Fn.split(':loadbalancer/',
                                                                   props.elbv2_public_lb.load_balancer_arn))))
        lb_id = Fn.select(2, Fn.split('/', Fn.select(1, Fn.split(':loadbalancer/',
                                                                 props.elbv2_public_lb.load_balancer_arn))))

        # Parse target group name and ID from the token ARN
        tg_name = hub_target_group.target_group_name
        tg_id = Fn.select(1, Fn.split('/', Fn.select(1, Fn.split(':targetgroup/', hub_target_group.target_group_arn))))

        # Create the resource label using Fn.join
        resource_label = Fn.join('', [
            'app/',
            lb_name,
            '/',
            lb_id,
            '/targetgroup/',
            tg_name,
            '/',
            tg_id
        ])

        # Create request count based scaling policy
        scaling_policy = applicationautoscaling.TargetTrackingScalingPolicy(
            self, "SeleniumHubRequestScaling",
            scaling_target=hub_scalable_target,
            target_value=1,  # Scale up when there's any request
            predefined_metric=applicationautoscaling.PredefinedMetric.ALB_REQUEST_COUNT_PER_TARGET,
            resource_label=resource_label,
            scale_in_cooldown=Duration.seconds(300),
            scale_out_cooldown=Duration.seconds(60)
        )


        '''

        # TODO: Use below if need separate scaling policy for hub
        '''
        # First, create the specific ALB metric for port 4444
        selenium_request_metric = cloudwatch.Metric(
            namespace="AWS/ApplicationELB",
            metric_name="RequestCountPerTarget",
            statistic="Sum",
            period=Duration.minutes(1),
            dimensions_map={
                "TargetGroup": hub_target_group.target_group_arn,
                "LoadBalancer": props.elbv2_public_lb.load_balancer_arn
            }
        )

        # Create the scaling policy with the specific metric
        scaling_policy = applicationautoscaling.TargetTrackingScalingPolicy(
            self, "SeleniumHubRequestScaling",
            scaling_target=hub_scalable_target,
            target_value=1.0,  # Scale when there's at least 1 request per target
            scale_in_cooldown=Duration.seconds(300),
            scale_out_cooldown=Duration.seconds(60),
            custom_metric=selenium_request_metric
        )
        '''

        # Add dependencies
        # scaling_policy.node.add_dependency(hub_target_group)
        # scaling_policy.node.add_dependency(props.elbv2_public_lb)

        return selenium_hub_service

    def create_node_resource(self,
                             identifier: str,
                             props: SharedStackProps,
                             image: str,
                             memory_limit: int = 512,
                             cpu: int = 256
                             ) -> ecs.FargateService:
        # Env parameters configured to connect back to selenium hub when new nodes gets added
        selenium_node_service = self.create_service(
            identifier=identifier,
            props=props,
            cluster=props.ecs_cluster,
            security_groups=[
                # props.ecs_security_group,
                props.sel_node_sg
            ],
            task_execution_role=props.task_execution_role,
            environment={
                # "HUB_PORT_4444_TCP_ADDR": props.elbv2_public_lb.load_balancer_dns_name,
                # "HUB_PORT_4444_TCP_PORT": str(props.selenium_hub_port),
                # "NODE_MAX_INSTANCES": str(props.selenium_node_max_instances),
                "SE_NODE_MAX_SESSIONS": str(props.selenium_node_max_sessions),
                "SE_NODE_OVERRIDE_MAX_SESSIONS": "false",
                "SE_VNC_NO_PASSWORD": "true",
                # "SE_EVENT_BUS_HOST": props.elbv2_public_lb.load_balancer_dns_name, # This is close to right
                "SE_EVENT_BUS_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
                # use the namespace service name
                # "SE_NODE_HOST": f"selenium_{identifier}.{props.ecs_default_cloud_map_namespace.namespace_name}",
                "SE_NODE_PORT": str(props.selenium_node_port),
                "SE_EVENT_BUS_PUBLISH_PORT": str(props.selenium_bus_publish_port),
                "SE_EVENT_BUS_SUBSCRIBE_PORT": str(props.selenium_bus_subscribe_port),
                "SE_SESSION_REQUEST_TIMEOUT": "300",
                "SE_NODE_SESSION_TIMEOUT": "600",
                # "SE_OPTS": '-debug',

                # NOTE: This is not needed since we are using --disable-dev-shm-usage flag which tells Chrome to use the disk for shared memory instead of /dev/shm
                # str(props.selenium_node_max_sessions)+'g', # 1 GB per selenium node session

                # TODO: For Video Recording - (Mount folder and upload to S3 Bucket???)
                #"SE_RECORD_VIDEO": "True",
                #"SE_VIDEO_FILE_NAME": "auto",
            },
            image=image + ':' + props.selenium_version,
            # TODO: Check what the entryp point and command should be for current selenium node chrome image
            entry_point=['sh', '-c'],
            command=[
                "PRIVATE=$(curl -s ${ECS_CONTAINER_METADATA_URI_V4}/task | jq -r '.Containers[0].Networks[0].IPv4Addresses[0]') ; export SE_NODE_HOST=\"$PRIVATE\" ; /opt/bin/entry_point.sh"
            ],
            ports=[props.selenium_node_port],
            memory_limit=memory_limit,
            cpu=cpu,
            log_driver=ecs.LogDriver.aws_logs(
                stream_prefix="selenium_node_logs",
                log_group=logs.LogGroup.from_log_group_arn(
                    self, "SeleniumNodeLogGroup",
                    log_group_arn=props.selenium_log_group_arn
                )
            ),
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL",
                         f"curl -f http://localhost:{props.selenium_node_port}/status || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(90)
            )
        )

        # TODO: Node shouldn't need a target group that is used by an ALB listener
        '''
        node_target_group = elbv2.ApplicationTargetGroup(
            self, f"selenium-{identifier}-target-group",
            target_group_name=f"sel-{identifier.replace('_','-')}-tg",
            vpc=props.ec2_vpc,
            port=props.selenium_node_port,
            targets=[selenium_node_service],
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check=elbv2.HealthCheck(
                path="/status",
                port=str(props.selenium_hub_port),
                interval=Duration.seconds(60),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
            deregistration_delay=Duration.seconds(0),  # No deregistration on node service
            # TODO: Review this deregistration value - does it allow for hub containers to process and auto scale as needed
        )
        '''

        # Create autoscaling policy
        node_scalable_target = self.create_scaling_policy(
            cluster_name=props.ecs_cluster.cluster_name,
            service_name=selenium_node_service.service_name,
            identifier=identifier,
            min_instances=props.min_instances,
            max_instances=props.max_instances

        )

        '''
        # Create a custom CloudWatch metric for Selenium session requests
        session_count_metric = cloudwatch.Metric(
            namespace="cqc-lem/selenium/node",
            metric_name="PendingSessionRequests",
            statistic="Sum",
            period=Duration.minutes(1)
        )

        # Scale based on pending session requests
        node_scalable_target.scale_to_track_metric(
            "NodeSessionScaling",
            custom_metric=session_count_metric,
            target_value=1,  # Scale up when there's any pending session
            scale_in_cooldown=Duration.minutes(5),
            scale_out_cooldown=Duration.minutes(1)
        )
        '''

        return selenium_node_service

    def create_service(
            self,
            identifier: str,
            cluster: ecs.Cluster,
            props: SharedStackProps,
            security_groups: List[ec2.SecurityGroup],
            image: str,
            ports: List[int],
            environment: Dict[str, str],
            task_execution_role: iam.IRole,
            log_driver: Optional[ecs.LogDriver] = None,
            entry_point: Optional[List[str]] = None,
            command: Optional[List[str]] = None,
            health_check: ecs.HealthCheck = None,
            memory_limit: int = 512,
            cpu: int = 256

    ) -> ecs.FargateService:
        """Create a Fargate service with the specified configuration.
        """
        # Task and container definition
        task_definition = ecs.FargateTaskDefinition(
            self,
            f'selenium-{identifier}-service-task-def',
            family=f'selenium_{identifier}',
            memory_limit_mib=memory_limit,
            cpu=cpu,
            task_role=task_execution_role
        )

        container_definition = task_definition.add_container(
            f'selenium_{identifier}-container',
            image=ecs.ContainerImage.from_registry(image),
            environment=environment,
            essential=True,
            logging=log_driver,
            entry_point=entry_point,
            command=command,
            # stop_timeout=Duration.seconds(120), # Do not add stop timeout as it kills the container
            health_check=health_check,
        )

        # Create a port name to port number map array
        port_name_map = [
            {
                "name": "selenium_hub",
                "port": props.selenium_hub_port
            },
            {
                "name": "selenium_node_chrome",
                "port": props.selenium_node_port
            },
            {
                "name": "selenium_bus_publish",
                "port": props.selenium_bus_publish_port
            },
            {
                "name": "selenium_bus_subscribe",
                "port": props.selenium_bus_subscribe_port
            }
        ]

        for port in ports:
            # find the port name from the port_name_map based on the port number
            port_map_item = next((item for item in port_name_map if item["port"] == port), {"name": "selenium_unknown"})

            # Add Port mapping
            container_definition.add_port_mappings(
                ecs.PortMapping(
                    container_port=port,
                    host_port=port,
                    protocol=ecs.Protocol.TCP,
                    name=port_map_item["name"]
                )
            )

        # Setup Fargate service
        return ecs.FargateService(
            self,
            f'selenium-{identifier}-service',
            service_name=f"selenium-{identifier}-service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            # VVV - Better for zero-downtime deployments - VVV
            min_healthy_percent=100,
            max_healthy_percent=200,
            health_check_grace_period=Duration.seconds(90),
            security_groups=security_groups,
            cloud_map_options=ecs.CloudMapOptions(
                name=f"selenium_{identifier}",
                dns_record_type=DnsRecordType.A,
                dns_ttl=Duration.seconds(60)
            ),
            # service_connect_configuration=ecs.ServiceConnectProps(
            #    namespace=props.ecs_default_cloud_map_namespace.namespace_name,
            #    services=[ecs.ServiceConnectService(
            #        port_mapping_name=f"selenium_{identifier}",  # Logical name for the service
            #        port=desired_port,  # Container port
            #    )]),
            enable_execute_command=False,
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(
                    capacity_provider='FARGATE',
                    base=1,
                    weight=1

                ),
                ecs.CapacityProviderStrategy(
                    capacity_provider='FARGATE_SPOT',
                    weight=4
                )
            ]
        )

    def create_scaling_policy(self,
                              service_name: str,
                              cluster_name: str,
                              identifier: str,
                              max_instances: int,
                              min_instances: int,
                              ) -> applicationautoscaling.ScalableTarget:
        # Create the scalable target
        scaling_target = applicationautoscaling.ScalableTarget(
            self, f'selenium-{identifier}-scalable-target',
            service_namespace=applicationautoscaling.ServiceNamespace.ECS,
            max_capacity=max_instances,
            min_capacity=min_instances,
            resource_id=f'service/{cluster_name}/{service_name}',
            scalable_dimension='ecs:service:DesiredCount'
        )

        scaling_policy = applicationautoscaling.TargetTrackingScalingPolicy(
            self, f"selenium-{identifier}-scalable-target-cpu-scaling-policy",
            policy_name=f"selenium-{identifier}-scalable-target-cpu-scaling",
            scaling_target=scaling_target,
            target_value=40.0,  # 40% CPU utilization target
            scale_in_cooldown=Duration.seconds(300),  # 5 minutes
            scale_out_cooldown=Duration.seconds(180),  # 3 minutes
            predefined_metric=applicationautoscaling.PredefinedMetric.ECS_SERVICE_AVERAGE_CPU_UTILIZATION
        )




        '''
        # Create the CloudWatch metric for CPU Max utilization
        worker_utilization_max_cpu_metric = cloudwatch.Metric(
            namespace='AWS/ECS',
            metric_name='CPUUtilization',
            statistic='max',
            period=Duration.minutes(1),
            dimensions_map={
                'ClusterName': cluster_name,
                'ServiceName': service_name
            }
        )

        # Create the scaling policy with steps
        scaling_target.scale_on_metric(
            f'selenium-{identifier}-cpu-max-step-metric-scaling',
            metric=worker_utilization_max_cpu_metric,
            adjustment_type=applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            scaling_steps=[
                applicationautoscaling.ScalingInterval(
                    change=-1,
                    upper=30
                ),
                applicationautoscaling.ScalingInterval(
                    change=1,
                    lower=80
                )
            ],
            cooldown=Duration.seconds(180)
        )

        # Create the CloudWatch metric for CPU Average utilization
        worker_utilization_avg_cpu_metric = cloudwatch.Metric(
            namespace='AWS/ECS',
            metric_name='CPUUtilization',
            statistic='avg',
            period=Duration.minutes(1),
            dimensions_map={
                'ClusterName': cluster_name,
                'ServiceName': service_name
            }
        )

        # Create the scaling policy with steps
        scaling_target.scale_on_metric(
            f'selenium-{identifier}-cpu-avg-step-metric-scaling',
            metric=worker_utilization_avg_cpu_metric,
            adjustment_type=applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            scaling_steps=[
                applicationautoscaling.ScalingInterval(
                    change=-1,
                    upper=10
                ),
                applicationautoscaling.ScalingInterval(
                    change=1,
                    lower=70
                )
            ],
            cooldown=Duration.seconds(180)
        )
        '''

        return scaling_target
