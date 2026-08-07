from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_applicationautoscaling as applicationautoscaling,
)
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_ecs as ecs,
)
from aws_cdk import (
    aws_logs as logs,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class CeleryWorkerStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 cpu: int = 512,
                 memory_limit_mib: int = 1024,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create a new Fargate Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, 'CeleryWorkerFargateTaskDef',
            family='celery_worker',
            cpu=cpu,
            memory_limit_mib=memory_limit_mib,
            task_role=props.task_execution_role

        )

        # Add a new volume to the Fargate Task Definition
        task_definition.add_volume(
            name=props.efs_volume_name,
            efs_volume_configuration=props.efs_volume_configuration,
        )

        # Add a new container to the Fargate Task Definition
        celery_worker_container = task_definition.add_container("CeleryWorkerContainer",
                                                                container_name="celery_worker",
                                                                image=ecs.ContainerImage.from_docker_image_asset(
                                                                    props.ecr_docker_asset),
                                                                command=["/start-celeryworker-solo"],
                                                                environment={
                                                                    # Env passed through props back to service ENV
                                                                    "OPENAI_API_KEY": props.open_api_key,
                                                                    "LI_CLIENT_ID": props.li_client_id,
                                                                    "LI_CLIENT_SECRET": props.li_client_secret,
                                                                    "LI_REDIRECT_URL": props.li_redirect_url,
                                                                    "LI_STATE_SALT": props.li_state_salt,
                                                                    "LI_API_VERSION": props.li_api_version,
                                                                    "PEXELS_API_KEY": props.pexels_api_key,
                                                                    "HF_TOKEN": props.hf_token,
                                                                    "REPLICATE_API_TOKEN": props.replicate_api_token,
                                                                    "RUNWAYML_API_SECRET": props.runwayml_api_secret,
                                                                    "TZ": props.tz,
                                                                    # "CELERY_ENABLE_UTC": "True",
                                                                    "CELERY_TIMEZONE": props.tz,
                                                                    # ENV set variables above
                                                                    "AWS_MYSQL_SECRET_NAME": props.ssm_myql_secret_name,
                                                                    "AWS_REGION": props.env.region,
                                                                    "CELERY_BROKER_URL": f"redis://{props.redis_url}:{props.redis_port}/0",
                                                                    "CELERY_RESULT_BACKEND": f"redis://{props.redis_url}:{props.redis_port}/1",
                                                                    "CELERY_QUEUE": "celery",
                                                                    # ^^^ Use this to define different priority of queues with more or less processing power
                                                                    # Use the AWS Device Farm by setting below env variables
                                                                    "DEVICE_FARM_PROJECT_ARN": props.device_farm_project_arn,
                                                                    "TEST_GRID_PROJECT_ARN": props.test_grid_project_arn,
                                                                    # "SELENIUM_HUB_HOST": props.elbv2_public_lb.load_balancer_dns_name, # Through the public load balancer
                                                                    "SELENIUM_HUB_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
                                                                    # Through the internal load balancer
                                                                    "SELENIUM_HUB_PORT": str(props.selenium_hub_port),
                                                                    "API_BASE_URL": props.api_base_url,
                                                                    "API_PORT": str(props.api_port),
                                                                    "HEADLESS_BROWSER": "FALSE",
                                                                    "CQC_LEM_CHECK_SCHEDULE_DELTA_MINUTES": "60",
                                                                    "CQC_LEM_POST_TIME_DELTA_MINUTES": "80"

                                                                    # TODO: Turn this to true in production

                                                                },
                                                                logging=ecs.LogDriver.aws_logs(
                                                                    stream_prefix="celery_worker_logs",
                                                                    log_group=logs.LogGroup(
                                                                        self, "CeleryWorkerLogGroup",
                                                                        log_group_name="/cqc-lem/celery_worker",
                                                                        retention=logs.RetentionDays.ONE_WEEK,
                                                                        removal_policy=RemovalPolicy.DESTROY
                                                                    )
                                                                ),
                                                                health_check=ecs.HealthCheck(
                                                                    command=["CMD-SHELL",
                                                                             "python -c 'import celery.bin.worker; exit(0)' || exit 1"]
                                                                    , interval=Duration.seconds(15),
                                                                    timeout=Duration.seconds(5),
                                                                    retries=1,
                                                                    start_period=Duration.seconds(30)

                                                                )
                                                                )

        # Add a new volume to the Fargate Task Definition
        celery_worker_container.add_mount_points(props.ecs_asset_mount_point)

        # Define needed security groups
        security_groups = [props.ecs_security_group]
        if props.sel_hub_sg:
            security_groups.append(props.sel_hub_sg)  # Need the hub security group to allow ingress

        # Create a service definitions and port mappings
        celery_worker_service = ecs.FargateService(
            self, 'CeleryWorkerService',
            cluster=props.ecs_cluster,
            enable_execute_command=False,  # Reduces metrics
            task_definition=task_definition,
            desired_count=1,
            # VVV - Better for zero-downtime deployments - VVV
            max_healthy_percent=200,
            min_healthy_percent=100,
            vpc_subnets=ec2.SubnetSelection(one_per_az=True, subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=security_groups,
            service_name="celery-worker-service",
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

        # TODO: already handled - remove - Allow the ECS Service to connect to the EFS
        celery_worker_service.connections.allow_from(props.efs_file_system, ec2.Port.tcp(2049))

        # TODO: already handled - remove - Allow the EFS to connect to the ECS Service
        celery_worker_service.connections.allow_to(props.efs_file_system, ec2.Port.tcp(2049))

        # Create the scalable target
        target = applicationautoscaling.ScalableTarget(
            self, 'celery-worker-scalable-target',
            service_namespace=applicationautoscaling.ServiceNamespace.ECS,
            min_capacity=1,
            max_capacity=50,  # TODO: Find a good number for max celery workers capacity
            resource_id=f'service/{props.ecs_cluster.cluster_name}/{celery_worker_service.service_name}',
            scalable_dimension='ecs:service:DesiredCount'
        )

        '''
        # Create the CloudWatch metric for CPU utilization
        worker_utilization_metric = cloudwatch.Metric(
            namespace='AWS/ECS',
            metric_name='CPUUtilization',
            statistic='max',
            period=Duration.minutes(1),
            dimensions_map={
                'ClusterName': props.ecs_cluster.cluster_name,
                'ServiceName': celery_worker_service.service_name
            }
        )

        # Create the scaling policy with steps
        target.scale_on_metric(
            f'celery-worker-step-metric-scaling',
            metric=worker_utilization_metric,
            adjustment_type=applicationautoscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            scaling_steps=[
                applicationautoscaling.ScalingInterval(
                    change=-1,
                    upper=30
                ),
                applicationautoscaling.ScalingInterval(
                    change=1,
                    lower=75
                )
            ],
            cooldown=Duration.seconds(180)
        )
        '''

        scaling_policy = applicationautoscaling.TargetTrackingScalingPolicy(
            self, "celery_worker-target-cpu-scaling-policy",
            policy_name="celery-worker-scalable-target-cpu-scaling",
            scaling_target=target,
            target_value=40.0,  # 40% CPU utilization target
            scale_in_cooldown=Duration.seconds(300),  # 5 minutes
            scale_out_cooldown=Duration.seconds(180),  # 3 minutes
            predefined_metric=applicationautoscaling.PredefinedMetric.ECS_SERVICE_AVERAGE_CPU_UTILIZATION
        )

        queue_length_metric = cloudwatch.Metric(
            namespace="cqc-lem/celery_queue/celery",
            metric_name="QueueLength",
            period=Duration.minutes(1),
            statistic="Maximum",
            dimensions_map={
                "QueueName": "celery"
            }
        )

        # Create the scaling policy with steps based on queue length thresholds
        target.scale_on_metric(
            'celery-worker-queue-length-scaling',
            metric=queue_length_metric,  # Use queue_length_metric instead of worker_utilization_metric
            adjustment_type=applicationautoscaling.AdjustmentType.EXACT_CAPACITY,
            evaluation_periods=2,  # Add this
            datapoints_to_alarm=2,  # Add this
            scaling_steps=[
                applicationautoscaling.ScalingInterval(
                    change=1,  # Minimum 1 worker when queue length < 5
                    upper=5
                ),
                applicationautoscaling.ScalingInterval(
                    change=3,  # 3 workers when queue length 5-25
                    lower=5,
                    upper=25
                ),
                applicationautoscaling.ScalingInterval(
                    change=8,  # 8 workers when queue length 25-50
                    lower=25,
                    upper=50
                ),
                applicationautoscaling.ScalingInterval(
                    change=15,  # 15 workers when queue length 50-100
                    lower=50,
                    upper=100
                ),
                applicationautoscaling.ScalingInterval(
                    change=25,  # 30 workers when queue length 100-150
                    lower=100,
                    upper=150
                ),
                applicationautoscaling.ScalingInterval(
                    change=50,  # 30 workers when queue length > 150
                    lower=150
                )
            ],
            cooldown=Duration.seconds(300)  # Reduce cooldown to make scaling more responsive
        )
