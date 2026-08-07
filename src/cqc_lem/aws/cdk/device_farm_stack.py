from aws_cdk import (
    Stack,
    aws_devicefarm as devicefarm,
    aws_ec2 as ec2,
)
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class DeviceFarmStack(Stack):
    def __init__(self, scope: Construct, construct_id: str,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create a Device Farm Project for Desktop Browser Testing
        project = devicefarm.CfnProject(
            self,
            "CQC-LEM-Browser-Project",
            name="CQCLEMBrowserProject",
            #vpc_config=devicefarm.CfnProject.VpcConfigProperty(
            #    security_group_ids=[props.ecs_security_group.security_group_id],
            #    subnet_ids=props.ec2_vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            #    vpc_id=props.ec2_vpc.vpc_id
            #)
        )

        # Create a test grid project
        test_grid_url = devicefarm.CfnTestGridProject(
            self,
            "CQCLEMBrowserGrid",
            name="CQCLEMBrowserGrid",
            description="Grid for Chrome browser automation",
            #vpc_config=devicefarm.CfnTestGridProject.VpcConfigProperty(
            #    security_group_ids=[props.ecs_security_group.security_group_id],
            #    subnet_ids=props.ec2_vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS).subnet_ids,
            #    vpc_id=props.ec2_vpc.vpc_id
            #)
        )

        # Add the output properties
        self.output_props = props.props.copy()
        self.output_props['device_farm_project_arn'] = project.attr_arn
        self.output_props['test_grid_project_arn'] = test_grid_url.attr_arn

    @property
    def outputs(self) -> SharedStackProps:
        return SharedStackProps(**self.output_props)
