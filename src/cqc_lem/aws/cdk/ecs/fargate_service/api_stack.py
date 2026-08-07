from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_elasticloadbalancingv2 as elbv2,
)
from aws_cdk import (
    aws_logs as logs,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class APIStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create a new Fargate Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, 'APIFargateTaskDef',
            family='api',
            cpu=512,
            memory_limit_mib=1024,
            task_role=props.task_execution_role,

        )

        # Add a new volume to the Fargate Task Definition
        task_definition.add_volume(
            name=props.efs_volume_name,
            efs_volume_configuration=props.efs_volume_configuration,
        )

        # Add a new container to the Fargate Task Definition
        api_key_container = task_definition.add_container("APIContainer",
                                                          container_name="api",
                                                          image=ecs.ContainerImage.from_docker_image_asset(
                                                              props.ecr_docker_asset),
                                                          command=["/start-fastapi-cloud"],  # Custom command
                                                          environment={
                                                              # Env passed through props back to service ENV
                                                              "OPENAI_API_KEY": props.open_api_key,
                                                              "LI_CLIENT_ID": props.li_client_id,
                                                              "LI_CLIENT_SECRET": props.li_client_secret,
                                                              "LI_REDIRECT_URL": props.li_redirect_url,
                                                              "LI_STATE_SALT": props.li_state_salt,
                                                              "LI_API_VERSION": props.li_api_version,
                                                              "TZ": props.tz,
                                                              # ENV set variables above
                                                              "AWS_MYSQL_SECRET_NAME": props.ssm_myql_secret_name,
                                                              "AWS_REGION": props.env.region,
                                                              "API_PORT": str(props.api_port),
                                                              "API_BASE_URL": props.api_base_url,
                                                              "CELERY_BROKER_URL": f"redis://{props.redis_url}:{props.redis_port}/0",
                                                              "CELERY_RESULT_BACKEND": f"redis://{props.redis_url}:{props.redis_port}/1",

                                                          },
                                                          port_mappings=[
                                                              ecs.PortMapping(
                                                                  container_port=props.api_port,
                                                                  host_port=props.api_port,
                                                                  name="api"  # Name of the port mapping
                                                              )
                                                          ],
                                                          logging=ecs.LogDriver.aws_logs(
                                                              stream_prefix="api_logs",
                                                              log_group=logs.LogGroup(
                                                                  self, "APILogGroup",
                                                                  log_group_name="/cqc-lem/api",
                                                                  retention=logs.RetentionDays.ONE_WEEK,
                                                                  removal_policy=RemovalPolicy.DESTROY
                                                              )
                                                          ),
                                                          health_check=ecs.HealthCheck(
                                                              command=["CMD-SHELL",
                                                                       f"curl -f http://localhost:{props.api_port}/health || exit 1"],
                                                              interval=Duration.seconds(15),
                                                              timeout=Duration.seconds(5),
                                                              retries=1,
                                                              start_period=Duration.seconds(30)
                                                          )
                                                          )

        # Add a new mount point to the Fargate Task Definition
        api_key_container.add_mount_points(props.ecs_asset_mount_point)

        # Create a service definitions and port mappings
        api_service = ecs.FargateService(
            self, 'APIService',
            cluster=props.ecs_cluster,
            enable_execute_command=False,  # Reduces metrics
            task_definition=task_definition,
            desired_count=1,
            # VVV - Better for zero-downtime deployments - VVV
            max_healthy_percent=200,
            min_healthy_percent=100,
            vpc_subnets=ec2.SubnetSelection(one_per_az=True, subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[props.ecs_security_group],
            service_connect_configuration=ecs.ServiceConnectProps(
                namespace=props.ecs_default_cloud_map_namespace.namespace_name,
                services=[ecs.ServiceConnectService(
                    port_mapping_name="api",  # Logical name for the service
                    port=props.api_port,  # Container port
                )]),
            service_name="api-service")

        # TODO: already handled - remove - Allow the ECS Service to connect to the EFS
        api_service.connections.allow_from(props.efs_file_system, ec2.Port.tcp(2049))

        # TODO: already handled - remove - Allow the EFS to connect to the ECS Service
        api_service.connections.allow_to(props.efs_file_system, ec2.Port.tcp(2049))

        target_group = elbv2.ApplicationTargetGroup(
            self, "APITargetGroup",
            target_group_name="api-target-group",
            vpc=props.ec2_vpc,
            port=props.api_port,
            targets=[api_service],
            # targets=[web_app_service.service],
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check=elbv2.HealthCheck(
                # path="/docs",
                path="/health",
                port=str(props.api_port),
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
            deregistration_delay=Duration.seconds(15),  # Very short
        )

        lb_rule = elbv2.ApplicationListenerRule(
            self, "APIListenerRule",
            listener=props.elbv2_api_listener,
            priority=20,
            action=elbv2.ListenerAction.forward(target_groups=[target_group]),
            conditions=[elbv2.ListenerCondition.path_patterns(["*"])],
        )
