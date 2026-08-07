import os

from aws_cdk import (
    Duration,
    NestedStack,
    RemovalPolicy,
)
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_lambda as _lambda,
)
from aws_cdk import (
    aws_lambda_python_alpha as _lambda_python_alpha,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk.aws_lambda import Tracing
from constructs import Construct


class LambdaStack(NestedStack):

    def __init__(self, scope: Construct, id: str,
                 vpc: ec2.Vpc,
                 redis_url: str,
                 redis_port: int,
                 redis_db: int,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        #  Create custom log group with retention
        log_group = logs.LogGroup(self, 'MyLogGroup',
                                  log_group_name='/cqc-lem/lambda/functions',
                                  retention=logs.RetentionDays.ONE_WEEK,
                                  removal_policy=RemovalPolicy.DESTROY  # Auto-delete when stack is destroyed
                                  )

        # Create Lambda function
        self.get_queue_message_count = _lambda_python_alpha.PythonFunction(self, "CeleryQueueMessageCountLambda",
                                                                           vpc=vpc,
                                                                           vpc_subnets=ec2.SubnetSelection(
                                                                               one_per_az=True,
                                                                               subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),

                                                                           entry=os.path.join(os.path.dirname(__file__),
                                                                                              "lambda"),
                                                                           # Path to the function
                                                                           index="get_redis_queue_message_count.py",
                                                                           # file name
                                                                           handler="lambda_handler",  # function name
                                                                           runtime=_lambda.Runtime.PYTHON_3_13,
                                                                           environment={
                                                                               'REDIS_URL': redis_url,
                                                                               'REDIS_PORT': str(redis_port),
                                                                               'REDIS_DB': str(redis_db)

                                                                           },
                                                                           timeout=Duration.seconds(10),
                                                                           log_group=log_group,
                                                                           insights_version=None,
                                                                           # Disable Lambda Insights
                                                                           tracing=Tracing.DISABLED,
                                                                           # Disable X-Ray tracing
                                                                           )

        # Output resources
        # CfnOutput(self, "MyLambdaFunctionARN", value=self.get_queue_message_count.function_arn)
