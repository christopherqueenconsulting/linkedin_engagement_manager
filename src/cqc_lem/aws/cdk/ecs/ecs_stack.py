from aws_cdk import CfnOutput, NestedStack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_servicediscovery as servicediscovery
from constructs import Construct

from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps


class EcsStack(NestedStack):

    def __init__(self, scope: Construct, id: str, vpc: ec2.Vpc,
                 props: SharedStackProps,
                 **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # ECS Security group, this will allow access from the Load Balancer and allow LAN access so that the
        # ECS containers can talk to each other on ingress ports
        self.ecs_security_group = ec2.SecurityGroup(self, "ECSSecurityGroup",
                                                    vpc=vpc,
                                                    description="ECS Security Group",
                                                    allow_all_outbound=True,
                                                    )

        # Security Group ingress rules for traffic among containers
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(props.api_port),
                                                 description=f"Port {props.api_port} for API traffic within VPC", )

        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(props.streamlit_port),
                                                 description=f"Port {props.streamlit_port} for Web traffic within VPC", )

        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                                 ec2.Port.tcp(props.celery_flower_port),
                                                 description=f"Port {props.celery_flower_port} for Celery Flower traffic within VPC", )

        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(props.redis_port),
                                                 description=f"Port {props.redis_port} for Redis traffic within VPC", )

        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(props.mysql_port),
                                                 description=f"Prot {props.mysql_port} for MySql traffic within VPC", )

        # Open up ports for selenium hub and node traffic
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                                 ec2.Port.tcp(props.selenium_hub_port),
                                                 'Port for Selenium Hub traffic within VPC')
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                                 ec2.Port.tcp(props.selenium_node_port),
                                                 'Port for Selenium Node traffic within VPC')
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                                 ec2.Port.tcp(props.selenium_bus_publish_port),
                                                 'Port for Selenium Bus Publish traffic within VPC')
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                                 ec2.Port.tcp(props.selenium_bus_subscribe_port),
                                                 'Port for Selenium Bus Subscribe traffic within VPC')

        # Allow outbound traffic to EFS
        self.ecs_security_group.add_egress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(2049),
                                                description="Allow outbound NFS traffic to EFS"
                                                )

        # Allow inbound traffic from EFS
        self.ecs_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(2049),
                                                 description="Allow inbound NFS traffic from EFS"
                                                 )

        # Creating a public load balancer security group
        self.public_lb_sg = ec2.SecurityGroup(self, "PublicLBSG", vpc=vpc, description="Public LB SG",
                                              allow_all_outbound=True)

        # Creating the public load balancer
        self.public_lb = elbv2.ApplicationLoadBalancer(self, "FrontendLB", vpc=vpc, internet_facing=True,
                                                       security_group=self.public_lb_sg,
                                                       vpc_subnets=ec2.SubnetSelection(
                                                           subnet_type=ec2.SubnetType.PUBLIC))

        self.public_lb.set_attribute(key="idle_timeout.timeout_seconds", value="30")

        self.public_lb_sg.add_ingress_rule(peer=ec2.Peer.any_ipv4(), connection=ec2.Port.tcp(80),
                                           description="Allow Web App traffic")

        self.web_listener = self.public_lb.add_listener("WebAppListener", port=80,
                                                        default_action=elbv2.ListenerAction.fixed_response(
                                                            status_code=200,
                                                            content_type="text/plain",
                                                            message_body="OK"))

        self.public_lb_sg.add_ingress_rule(peer=ec2.Peer.any_ipv4(), connection=ec2.Port.tcp(props.api_port),
                                           description="Allow API traffic")
        self.api_listener = self.public_lb.add_listener("APIListener", port=props.api_port,
                                                        protocol=elbv2.ApplicationProtocol.HTTP,
                                                        default_action=elbv2.ListenerAction.fixed_response(
                                                            status_code=200,
                                                            content_type="text/plain",
                                                            message_body="OK"))

        self.public_lb_sg.add_ingress_rule(peer=ec2.Peer.any_ipv4(), connection=ec2.Port.tcp(props.celery_flower_port),
                                           description="Allow Celery Flower traffic")
        self.flower_listener = self.public_lb.add_listener("CeleryFlowerListener", port=props.celery_flower_port,
                                                           protocol=elbv2.ApplicationProtocol.HTTP,
                                                           default_action=elbv2.ListenerAction.fixed_response(
                                                               status_code=200,
                                                               content_type="text/plain",
                                                               message_body="OK"))

        self.selenium_hub_listener = self.public_lb.add_listener("SeleniumHubListener", port=props.selenium_hub_port,
                                                                 protocol=elbv2.ApplicationProtocol.HTTP,
                                                                 default_action=elbv2.ListenerAction.fixed_response(
                                                                     status_code=200,
                                                                     content_type="text/plain",
                                                                     message_body="OK"))

        self.selenium_bus_publish_listener = self.public_lb.add_listener("SeleniumBusPublishListener",
                                                                         port=props.selenium_bus_publish_port,
                                                                         protocol=elbv2.ApplicationProtocol.HTTP,
                                                                         default_action=elbv2.ListenerAction.fixed_response(
                                                                             status_code=200,
                                                                             content_type="text/plain",
                                                                             message_body="OK"))

        self.selenium_bus_subscribe_listener = self.public_lb.add_listener("SeleniumBusSubscribeListener",
                                                                           port=props.selenium_bus_subscribe_port,
                                                                           protocol=elbv2.ApplicationProtocol.HTTP,
                                                                           default_action=elbv2.ListenerAction.fixed_response(
                                                                               status_code=200,
                                                                               content_type="text/plain",
                                                                               message_body="OK"))

        # Creating the ECS Cluster and the cloud map namespace
        self.ecs_cluster = ecs.Cluster(self, "ECSCluster",
                                       vpc=vpc,
                                       cluster_name="cqc-lem-cluster",
                                       container_insights=False,
                                       enable_fargate_capacity_providers=True
                                       )

        # Allow inbound and outbound traffic to the ECS cluster on NFS port 2049
        self.ecs_cluster.connections.allow_from(ec2.Peer.any_ipv4(), ec2.Port.tcp(2049))
        self.ecs_cluster.connections.allow_to(ec2.Peer.any_ipv4(), ec2.Port.tcp(2049))

        self.default_cloud_map_namespace = self.ecs_cluster.add_default_cloud_map_namespace(name="cqc-lem.local",
                                                                                            use_for_service_connect=True,
                                                                                            type=servicediscovery.NamespaceType.DNS_PRIVATE)


        CfnOutput(self, "Load Balancer URL", value=f"http://{self.public_lb.load_balancer_dns_name}")
