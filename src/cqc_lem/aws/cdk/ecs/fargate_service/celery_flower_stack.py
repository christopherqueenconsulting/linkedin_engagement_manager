from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    TimeZone,
    aws_applicationautoscaling as appscaling,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_logs as logs,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class CeleryFlowerStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create a new Fargate Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, 'CeleryFlowerFargateTaskDef',
            family='celery_flower',
            cpu=512,  # TODO: FInd out why celery flower needs so much CPU
            memory_limit_mib=1024,  # TODO: Find out why celery flower needs so much memory
            task_role=props.task_execution_role

        )

        # Add a new volume to the Fargate Task Definition
        task_definition.add_volume(
            name=props.efs_volume_name,
            efs_volume_configuration=props.efs_volume_configuration,
        )

        # Add a new container to the Fargate Task Definition
        celery_flower_container = task_definition.add_container("CeleryFlowerContainer",
                                                                container_name="celery_flower",
                                                                image=ecs.ContainerImage.from_docker_image_asset(
                                                                    props.ecr_docker_asset),
                                                                command=["/start-flower-no-wait"],  # Custom command
                                                                environment={
                                                                    "OPENAI_API_KEY": props.open_api_key,
                                                                    "AWS_MYSQL_SECRET_NAME": props.ssm_myql_secret_name,
                                                                    "AWS_REGION": props.env.region,
                                                                    "CELERY_BROKER_URL": f"redis://{props.redis_url}:{props.redis_port}/0",
                                                                    "CELERY_RESULT_BACKEND": f"redis://{props.redis_url}:{props.redis_port}/1",
                                                                    "CELERY_FLOWER_PORT": str(props.celery_flower_port),
                                                                    "CELERY_FLOWER_STATE_SAVE_INTERVAL": str(
                                                                        1000 * 60 * 15),  # 15 minutes
                                                                    "TZ": props.tz,
                                                                    # "CELERY_ENABLE_UTC": "True",
                                                                    "CELERY_TIMEZONE": props.tz,
                                                                    "FLOWER_UNAUTHENTICATED_API": "True",
                                                                    "FLOWER_PERSISTENT": "True",
                                                                    "FLOWER_SAVE_STATE_INTERVAL": str(1000 * 60 * 15)
                                                                    # 15 minutes

                                                                },
                                                                port_mappings=[
                                                                    ecs.PortMapping(
                                                                        container_port=props.celery_flower_port,
                                                                        host_port=props.celery_flower_port,
                                                                        name="celery_flower"  # Name of the port mapping
                                                                    )
                                                                ],
                                                                logging=ecs.LogDriver.aws_logs(
                                                                    stream_prefix="celery_flower_logs",
                                                                    log_group=logs.LogGroup(
                                                                        self, "CeleryFlowerLogGroup",
                                                                        log_group_name="/cqc-lem/celery_flower",
                                                                        retention=logs.RetentionDays.ONE_WEEK,
                                                                        removal_policy=RemovalPolicy.DESTROY
                                                                    )
                                                                ),
                                                                health_check=ecs.HealthCheck(
                                                                    command=["CMD-SHELL",
                                                                             f"curl -f http://localhost:{props.celery_flower_port}/healthcheck || exit 1"],
                                                                    interval=Duration.seconds(30),
                                                                    timeout=Duration.seconds(10),
                                                                    retries=5,
                                                                    start_period=Duration.seconds(90)
                                                                    # Flower needs time to connect to broker

                                                                )
                                                                )

        # Add a new volume to the Fargate Task Definition
        celery_flower_container.add_mount_points(props.ecs_celery_flower_data_mount_point)

        # Create a service definitions and port mappings
        celery_flower_service = ecs.FargateService(
            self, 'CeleryFlowerService',
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
                    port_mapping_name="celery_flower",  # Logical name for the service
                    port=props.celery_flower_port,  # Container port
                )]),
            service_name="celery-flower-service")

        # TODO: already handled - remove - Allow the ECS Service to connect to the EFS
        celery_flower_service.connections.allow_from(props.efs_file_system, ec2.Port.tcp(2049))

        # TODO: already handled - remove - Allow the EFS to connect to the ECS Service
        celery_flower_service.connections.allow_to(props.efs_file_system, ec2.Port.tcp(2049))

        target_group = elbv2.ApplicationTargetGroup(
            self, "CeleryFlowerTargetGroup",
            target_group_name="celery-flower-target-group",
            vpc=props.ec2_vpc,
            port=props.celery_flower_port,
            targets=[celery_flower_service],
            # targets=[celery_flower_service.service],
            target_type=elbv2.TargetType.IP,
            protocol=elbv2.ApplicationProtocol.HTTP,
            health_check=elbv2.HealthCheck(
                path="/healthcheck",
                port=str(props.celery_flower_port),
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
            deregistration_delay=Duration.seconds(30),  # Good Balance
        )

        lb_rule = elbv2.ApplicationListenerRule(
            self, "CeleryFlowerListenerRule",
            listener=props.elbv2_flower_listener,
            priority=40,
            action=elbv2.ListenerAction.forward(target_groups=[target_group]),
            conditions=[elbv2.ListenerCondition.path_patterns(["*"])],
        )

        # Add scheduled scaling to the Fargate service
        scaling = celery_flower_service.auto_scale_task_count(
            min_capacity=0,
            max_capacity=1  # Or whatever your max capacity should be
        )

        # Scale down to 0 at 10 AM every day
        scaling.scale_on_schedule(
            id="CeleryFlowerScaleDown",
            time_zone=TimeZone.AMERICA_NEW_YORK,
            schedule=appscaling.Schedule.cron(
                hour="10",
                minute="0"
            ),
            min_capacity=0,
            max_capacity=0
        )

        # Scale up to 1 at 8:30 AM every day
        scaling.scale_on_schedule(
            id="CeleryFlowerScaleUp",
            time_zone=TimeZone.AMERICA_NEW_YORK,
            schedule=appscaling.Schedule.cron(
                hour="8",
                minute="30"
            ),
            min_capacity=1,
            max_capacity=1
        )
