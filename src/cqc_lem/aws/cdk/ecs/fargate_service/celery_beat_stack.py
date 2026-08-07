from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_logs as logs,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class CeleryBeatStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create a new Fargate Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, 'CeleryBeatFargateTaskDef',
            family='celery_beat',
            cpu=256,
            memory_limit_mib=512,
            task_role=props.task_execution_role

        )

        # Add a new volume to the Fargate Task Definition
        task_definition.add_volume(
            name=props.efs_volume_name,
            efs_volume_configuration=props.efs_volume_configuration,
        )

        # Add a new container to the Fargate Task Definition
        celery_beat_container = task_definition.add_container("CeleryBeatContainer",
                                                              container_name="celery_beat",
                                                              image=ecs.ContainerImage.from_docker_image_asset(
                                                                  props.ecr_docker_asset),
                                                              command=["/start-celerybeat"],  # Custom command
                                                              environment={
                                                                  # Env passed through props back to service ENV
                                                                  "OPENAI_API_KEY": props.open_api_key,
                                                                  "TZ": props.tz,
                                                                  #"CELERY_ENABLE_UTC": "True",
                                                                  "CELERY_TIMEZONE" : props.tz,
                                                                  "PURGE_TASKS": "True" if props.purge_tasks else "False",
                                                                  "CLEAR_SELENIUM_SESSIONS": "True" if props.clear_selenium_sessions else "False",
                                                                  # ENV set variables above
                                                                  "AWS_MYSQL_SECRET_NAME": props.ssm_myql_secret_name,
                                                                  "AWS_REGION": props.env.region,
                                                                  "CELERY_BROKER_URL": f"redis://{props.redis_url}:{props.redis_port}/0",
                                                                  "CELERY_RESULT_BACKEND": f"redis://{props.redis_url}:{props.redis_port}/1",
                                                                  "CELERY_QUEUE": "celery",
                                                                  # Use the AWS Device Farm by setting below env variables
                                                                  "DEVICE_FARM_PROJECT_ARN": props.device_farm_project_arn,
                                                                  "TEST_GRID_PROJECT_ARN": props.test_grid_project_arn,
                                                                  # "SELENIUM_HUB_HOST": props.elbv2_public_lb.load_balancer_dns_name, # Through the public load balancer
                                                                  "SELENIUM_HUB_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
                                                                  # Through the internal load balancer
                                                                  "SELENIUM_HUB_PORT": str(props.selenium_hub_port),
                                                                  # ^^^ Use this to define different priority of queues with more or less processing power
                                                                  "CQC_LEM_CHECK_SCHEDULE_DELTA_MINUTES": "30",
                                                                  "CQC_LEM_POST_TIME_DELTA_MINUTES": "50"

                                                              },
                                                              logging=ecs.LogDriver.aws_logs(
                                                                  stream_prefix="celery_beat_logs",
                                                                  log_group=logs.LogGroup(
                                                                      self, "CeleryBeatLogGroup",
                                                                      log_group_name="/cqc-lem/celery_beat",
                                                                      retention=logs.RetentionDays.ONE_WEEK,
                                                                      removal_policy=RemovalPolicy.DESTROY
                                                                  )
                                                              ),
                                                              health_check=ecs.HealthCheck(
                                                                  command=["CMD-SHELL",
                                                                           "test -f celerybeat-schedule || exit 1"
                                                                           ],
                                                                  interval=Duration.seconds(15),
                                                                  timeout=Duration.seconds(5),
                                                                  retries=1,
                                                                  start_period=Duration.seconds(60),

                                                              )
                                                              )

        # Add a new volume to the Fargate Task Definition
        celery_beat_container.add_mount_points(props.ecs_asset_mount_point)

        # Create a service definitions and port mappings
        celery_beat_service = ecs.FargateService(
            self, 'CeleryBeatService',
            cluster=props.ecs_cluster,
            enable_execute_command=False,  # Reduces metrics
            task_definition=task_definition,
            desired_count=1,
            # VVV - Better for zero-downtime deployments - VVV
            max_healthy_percent=200,
            min_healthy_percent=100,
            vpc_subnets=ec2.SubnetSelection(one_per_az=True, subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[props.ecs_security_group],
            service_name="celery-beat-service",
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(
                    capacity_provider='FARGATE',
                    weight=1

                )

            ]

        )

        # Ensure the service can't be scaled up accidentally
        celery_beat_service.auto_scale_task_count(
            min_capacity=1,
            max_capacity=1
        )

        # TODO: already handled - remove - Allow the ECS Service to connect to the EFS
        celery_beat_service.connections.allow_from(props.efs_file_system, ec2.Port.tcp(2049))

        # TODO: already handled - remove - Allow the EFS to connect to the ECS Service
        celery_beat_service.connections.allow_to(props.efs_file_system, ec2.Port.tcp(2049))
