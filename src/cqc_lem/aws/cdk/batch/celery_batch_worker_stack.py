from aws_cdk import (
    CfnOutput,
    Duration,
    Size,
    Stack,
)
from aws_cdk import (
    aws_batch as batch,
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
    aws_events as events,
)
from aws_cdk import (
    aws_events_targets as targets,
)
from aws_cdk import (
    aws_logs as logs,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class CeleryBatchWorkerStack(Stack):

    def __init__(self, scope: Construct, id: str,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create AWS Batch Job Queue
        self.celery_batch_job_queue = batch.JobQueue(self, "CeleryBatchWorkerJobQueue",
                                                     job_queue_name="celery-batch-worker-job-queue"
                                                     )

        batch_env_count = 3

        for i in range(batch_env_count):
            fargate_spot_environment = batch.FargateComputeEnvironment(self, f"CeleryBatchWorkerFargateEnv{i}",
                                                                       vpc=props.ec2_vpc,
                                                                       vpc_subnets=ec2.SubnetSelection(
                                                                           subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                                                       compute_environment_name=f"CeleryBatchWorkerFargateEnv{i}",
                                                                       security_groups=[props.ecs_security_group],
                                                                       spot=True if i > 0 else False,
                                                                       maxv_cpus=49 if i > 0 else 1,
                                                                       # Set this ^^^ based on maximum expected concurrent jobs                                                                   minvCpus=0,
                                                                       terminate_on_update=False,
                                                                       update_timeout=Duration.minutes(3)

                                                                       )

            self.celery_batch_job_queue.add_compute_environment(fargate_spot_environment, batch_env_count - i)

        # Add a new batch container to the Fargate Task Definition
        container = batch.EcsFargateContainerDefinition(self, "CeleryBatchWorkerFargateContainer",
                                                        image=ecs.ContainerImage.from_docker_image_asset(
                                                            props.ecr_docker_asset),
                                                        command=["/start-celeryworker-solo"],
                                                        cpu=1,  # TODO: Put back to 1 if service doesnt start
                                                        memory=Size.gibibytes(2),
                                                        # TODO": Put back to 2048 if service doesnt start
                                                        execution_role=props.task_execution_role,
                                                        environment={
                                                            # Env passed through props back to service ENV
                                                            "OPENAI_API_KEY": props.open_api_key,
                                                            "LI_CLIENT_ID": props.li_client_id,
                                                            "LI_CLIENT_SECRET": props.li_client_secret,
                                                            "LI_REDIRECT_URI": props.li_redirect_url,
                                                            "LI_STATE_SALT": props.li_state_salt,
                                                            "LI_API_VERSION": props.li_api_version,
                                                            "PEXELS_API_KEY": props.pexels_api_key,
                                                            "HF_TOKEN": props.hf_token,
                                                            "REPLICATE_API_TOKEN": props.replicate_api_token,
                                                            "RUNWAYML_API_SECRET": props.runwayml_api_secret,
                                                            "TZ":props.tz,
                                                            # ENV set variables above
                                                            "AWS_MYSQL_SECRET_NAME": props.ssm_myql_secret_name,
                                                            "AWS_REGION": props.env.region,
                                                            "CELERY_BROKER_URL": f"redis://{props.redis_url}:{props.redis_port}/0",
                                                            "CELERY_RESULT_BACKEND": f"redis://{props.redis_url}:{props.redis_port}/1",
                                                            "CELERY_QUEUE": "celery",
                                                            # ^^^ Use this to define different priority of queues with more or less processing power
                                                            # "SELENIUM_HUB_HOST": props.elbv2_public_lb.load_balancer_dns_name, # Through the public load balancer
                                                            "SELENIUM_HUB_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
                                                            # Through the internal load balancer
                                                            "SELENIUM_HUB_PORT": str(props.selenium_hub_port),
                                                            "HEADLESS_BROWSER": "FALSE"
                                                            # TODO: Turn this to true in production


                                                        },
                                                        # TODO: Volume added but no mount point. Need to verify this for assets
                                                        # Add a new volume to the container
                                                        volumes=[batch.EcsVolume.efs(
                                                            name="CeleryBatchWorkerVolume",
                                                            file_system=props.efs_file_system,
                                                            container_path=props.efs_app_assets_path,
                                                            access_point_id=props.efs_access_point.access_point_id,
                                                            enable_transit_encryption=True,
                                                            use_job_role=True
                                                        )],
                                                        job_role=props.task_execution_role,
                                                        logging=ecs.LogDriver.aws_logs(
                                                            stream_prefix="celery_batch_worker_logs",
                                                            log_group=logs.LogGroup.from_log_group_arn(
                                                                self, "CeleryBatchWorkerLogGroup",
                                                                log_group_arn=props.celery_worker_batch_log_group_arn
                                                            )
                                                        )

                                                        )

        # Create Batch job definition
        celery_job_def = batch.EcsJobDefinition(self, "CeleryBatchWorkerJobDef",
                                                job_definition_name='celery_batch_worker',
                                                container=container,
                                                timeout=Duration.minutes(15)
                                                # The time the job has to complete before it will be terminated
                                                )

        # Define alarm thresholds and corresponding batch sizes
        # Define scaling tiers with alarm configurations
        scaling_configs = [
            {
                "threshold": 1,
                "batch_size": 1,
                "datapoints": 1,  # Quick response for super small queues
                "periods": 1
            },
            {
                "threshold": 10,
                "batch_size": 5,
                "datapoints": 1,  # Quick response for small queues
                "periods": 1
            },
            {
                "threshold": 25,
                "batch_size": 10,
                "datapoints": 2,  # More conservative
                "periods": 2
            },
            {
                "threshold": 50,
                "batch_size": 25,
                "datapoints": 3,  # Most conservative
                "periods": 3  # Ensure sustained load before launching larger batches
            }
        ]

        # Create alarms for each threshold
        for config in scaling_configs:
            alarm = cloudwatch.Alarm(
                self, f"RedisQueueAlarmThreshold{config['threshold']}",
                metric=cloudwatch.Metric(
                    namespace="cqc-lem/celery_queue/celery",  # TODO: Need this somewhere central
                    metric_name="QueueLength",
                    period=Duration.minutes(1),
                    statistic="Maximum"
                ),
                threshold=config['threshold'],
                evaluation_periods=config['periods'],
                datapoints_to_alarm=config['datapoints'],  # Must have x periods in alarm state
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            )

            # Create EventBridge rule for each threshold
            rule = events.Rule(
                self, f"CeleryBatchWorkerRuleThreshold{config['threshold']}",
                event_pattern=events.EventPattern(
                    source=["aws.cloudwatch"],
                    detail_type=["CloudWatch Alarm State Change"],
                    detail={
                        "alarmName": [alarm.alarm_name],
                        "state": {
                            "value": ["ALARM"]
                        }
                    }
                )
            )

            # Add Batch job target with corresponding batch size
            rule.add_target(targets.BatchJob(
                job_queue_scope=self,
                job_queue_arn=self.celery_batch_job_queue.job_queue_arn,
                job_definition_arn=celery_job_def.job_definition_arn,
                job_definition_scope=self,
                job_name=f"celery-workers-batch-{config['threshold']}",
                size=config['batch_size'] if config['batch_size'] > 1 else None
            ))

        # Output resources
        CfnOutput(self, "BatchCeleryJobQueue", value=self.celery_batch_job_queue.job_queue_name)
        CfnOutput(self, "BatchCeleryJobDefinition", value=celery_job_def.job_definition_name)
