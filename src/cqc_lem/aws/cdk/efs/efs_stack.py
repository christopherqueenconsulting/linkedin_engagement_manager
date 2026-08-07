from aws_cdk import (
    NestedStack,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_iam as iam,
)
from constructs import Construct

from cqc_lem.aws.cdk.efs import CONTAINER_APP_ASSETS_PATH, EFS_VOLUME_NAME


class EFSStack(NestedStack):

    def __init__(self, scope: Construct, id: str, vpc: ec2.Vpc, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # Create an Amazon Elastic File System (EFS), with the logical ID
        self.file_system = efs.FileSystem(
            self, 'EFS',
            vpc=vpc,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
        )

        # Create an Access Point for the EFS, with the logical ID CDK-efs-sample-AccessPoint
        self.efs_access_point = efs.AccessPoint(
            self, 'AccessPoint',
            file_system=self.file_system,
        )

        # Create a new EFS volume configuration for the ECS Task
        self.efs_volume_configuration = ecs.EfsVolumeConfiguration(
            file_system_id=self.file_system.file_system_id,

            # The logical ID of the Access Point to use.
            # This is a string, not an ARN.
            authorization_config=ecs.AuthorizationConfig(
                access_point_id=self.efs_access_point.access_point_id,
                iam='ENABLED',
            ),
            transit_encryption='ENABLED',
        )

        # Create a new IAM Role for the ECS Task
        self.EFSTaskRole = iam.Role(
            self, 'EFSTaskRole',
            assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com').with_conditions({
                "StringEquals": {
                    "aws:SourceAccount": Stack.of(self).account
                },
                "ArnLike": {
                    "aws:SourceArn": "arn:aws:ecs:" + Stack.of(self).region + ":" + Stack.of(self).account + ":*"
                },
            }),
        )

        # Attach a managed policy to the IAM Role
        self.EFSTaskRole.attach_inline_policy(
            iam.Policy(self, 'EFSPolicy',
                       statements=[
                           iam.PolicyStatement(
                               effect=iam.Effect.ALLOW,
                               resources=['*'],
                               actions=[
                                   "ecr:GetAuthorizationToken",
                                   "ec2:DescribeAvailabilityZones"
                               ]
                           ),
                           iam.PolicyStatement(
                               sid='AllowEfsAccess',
                               effect=iam.Effect.ALLOW,
                               resources=['*'],
                               actions=[
                                   'elasticfilesystem:ClientRootAccess',
                                   'elasticfilesystem:ClientWrite',
                                   'elasticfilesystem:ClientMount',
                                   'elasticfilesystem:DescribeMountTargets'
                               ]
                           )
                       ]
                       )
        )

        # Create container mount point for Fargate Task Definitions
        self.efs_volume_name = EFS_VOLUME_NAME
        self.efs_app_assets_path = CONTAINER_APP_ASSETS_PATH
        self.asset_mount_point = ecs.MountPoint(
            container_path=CONTAINER_APP_ASSETS_PATH,
            source_volume=self.efs_volume_name,
            read_only=False,
        )
        self.celery_flower_data_mount_point = ecs.MountPoint(
            container_path='/data',
            source_volume=self.efs_volume_name,
            read_only=False,
        )

        # TODO: Add mount points to Selenium Nodes
        self.selenium_video_mount_point = ecs.MountPoint(
            container_path='/opt/selenium/assets',
            source_volume=self.efs_volume_name,
            read_only=False,
        )



        #CfnOutput(self, "EFSFileSystemId", value=self.file_system.file_system_id)
        #CfnOutput(self, "EFSAccessPointId", value=self.efs_access_point.access_point_id)
