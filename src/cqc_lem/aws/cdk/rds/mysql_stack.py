from aws_cdk import (
    CfnOutput,
    Duration,
    NestedStack,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_rds as rds,
)
from constructs import Construct


class MySQLStack(NestedStack):

    def __init__(self, scope: Construct, id: str,
                 vpc: ec2.Vpc,
                 mysql_port: int,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create RDS MySQL Database
        db_instance = rds.DatabaseInstance(self, "MySQLDatabase",
                                           engine=rds.DatabaseInstanceEngine.mysql(
                                               version=rds.MysqlEngineVersion.VER_8_0_40
                                           ),
                                           vpc=vpc,
                                           # vpc_subnets makes this public to the web
                                           vpc_subnets=ec2.SubnetSelection(
                                               subnet_type=ec2.SubnetType.PUBLIC,
                                           ),
                                           allocated_storage=20,
                                           max_allocated_storage=100,
                                           multi_az=False,
                                           port=mysql_port,
                                           instance_type=ec2.InstanceType.of(
                                               ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO
                                           ),
                                           # credentials=rds.Credentials.from_generated_secret("admin"),  # Optional - will default to 'admin' username and generated password
                                           database_name="linkedin_manager",
                                           removal_policy=RemovalPolicy.DESTROY,
                                           deletion_protection=False,
                                           monitoring_interval=Duration.seconds(0),  # Disable enhanced monitoring
                                           enable_performance_insights=False,  # Disable performance insights
                                           cloudwatch_logs_exports=[]  # Disable log exports

                                           )

        # Allow connections from any IP address to the mysql port
        db_instance.connections.allow_from_any_ipv4(
            ec2.Port.tcp(mysql_port),
            description="Mysql port for connection"
        )

        self.db_instance_endpoint_address = db_instance.db_instance_endpoint_address
        self.myql_secret_name = db_instance.secret.secret_name

        # Output resources
        CfnOutput(self, "DBInstanceEndpoint", value=db_instance.db_instance_endpoint_address)
        #CfnOutput(self, "DBSecretName", value=db_instance.secret.secret_name)
        #CfnOutput(self, "DBInstanceARN", value=db_instance.instance_arn)
