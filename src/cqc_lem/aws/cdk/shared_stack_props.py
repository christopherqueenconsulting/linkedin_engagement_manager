import re

from aws_cdk import (
    Environment,
    StackProps,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_efs as efs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
)
from aws_cdk.aws_ecr_assets import DockerImageAsset
from aws_cdk.aws_lambda import IFunction
from aws_cdk.aws_servicediscovery import INamespace


class SharedStackProps(StackProps):
    props: dict = {}

    def __init__(self,

                 ec2_vpc: ec2.Vpc = None,
                 ecr_docker_asset: DockerImageAsset = None,
                 rds_db_instance_endpoint_address: str = None,
                 ssm_myql_secret_name: str = None,
                 efs_file_system: efs.FileSystem = None,
                 efs_access_point: efs.AccessPoint = None,
                 efs_volume_configuration: ecs.EfsVolumeConfiguration = None,
                 efs_task_role: iam.Role = None,
                 efs_volume_name: str = None,
                 efs_app_assets_path: str = None,
                 ecs_asset_mount_point: ecs.MountPoint = None,
                 ecs_celery_flower_data_mount_point: ecs.MountPoint = None,
                 ecs_task_iam_role: iam.Role = None,
                 task_execution_role: iam.Role = None,
                 ecs_cluster: ecs.Cluster = None,
                 ecs_default_cloud_map_namespace: INamespace = None,
                 ecs_security_group: ec2.SecurityGroup = None,
                 sel_hub_sg: ec2.SecurityGroup = None,
                 sel_node_sg: ec2.SecurityGroup = None,
                 ec2_public_lb_sg: ec2.SecurityGroup = None,
                 elbv2_public_lb: elbv2.ApplicationLoadBalancer = None,
                 elbv2_web_listener: elbv2.ApplicationListener = None,
                 elbv2_api_listener: elbv2.ApplicationListener = None,
                 elbv2_flower_listener: elbv2.ApplicationListener = None,
                 elbv2_selenium_hub_listener: elbv2.ApplicationListener = None,
                 elbv2_selenium_bus_publish_listener: elbv2.ApplicationListener = None,
                 elbv2_selenium_bus_subscribe_listener: elbv2.ApplicationListener = None,
                 sqs_queue_url: str = None,
                 redis_cluster_id: str = None,
                 redis_url: str = None,
                 env: Environment = None,
                 lambda_get_redis_queue_message_count: IFunction = None,
                 celery_worker_batch_log_group_arn: str = None,
                 selenium_log_group_arn: str = None,
                 api_base_url: str = None,
                 selenium_node_max_instances: int = 4,
                 selenium_node_max_sessions: int = 4,
                 min_instances: int = 1,
                 max_instances: int = 4,  # TODO: Increase this to 10 or more once resources sizes are set correctly
                 # selenium_version: str = "4.26.0-20241101",
                 selenium_version: str = "latest",
                 api_port: int = 8000,
                 streamlit_port: int = 8501,
                 celery_flower_port: int = 8555,
                 redis_port: int = 6379,
                 mysql_port: int = 3306,
                 selenium_hub_port: int = 4444,
                 selenium_bus_publish_port: int = 4442,
                 selenium_bus_subscribe_port: int = 4443,
                 selenium_node_port: int = 5555,
                 open_api_key: str = None,
                 streamlit_email: str = None,
                 li_client_id: str = None,
                 li_client_secret: str = None,
                 li_state_salt: str = None,
                 li_api_version: str = None,
                 pexels_api_key: str = None,
                 hf_token: str = None,
                 replicate_api_token: str = None,
                 runwayml_api_secret: str = None,
                 tz: str = None,
                 purge_tasks: bool = False,
                 clear_selenium_sessions: bool = False,
                 device_farm_project_arn: str = "103658592769",
                 test_grid_project_arn: str = "52615a58-16b0-461e-9130-e9a6272423c7",

                 **kwargs):
        super().__init__(env=env)
        self.props = kwargs
        self.props['env'] = env
        self.props['ec2_vpc'] = ec2_vpc
        self.props['ecr_docker_asset'] = ecr_docker_asset
        self.props['rds_db_instance_endpoint_address'] = rds_db_instance_endpoint_address
        self.props['ssm_myql_secret_name'] = ssm_myql_secret_name
        self.props['efs_file_system'] = efs_file_system
        self.props['efs_access_point'] = efs_access_point
        self.props['efs_volume_configuration'] = efs_volume_configuration
        self.props['efs_task_role'] = efs_task_role
        self.props['efs_volume_name'] = efs_volume_name
        self.props['efs_app_assets_path'] = efs_app_assets_path
        self.props['ecs_asset_mount_point'] = ecs_asset_mount_point
        self.props['ecs_celery_flower_data_mount_point'] = ecs_celery_flower_data_mount_point
        self.props['ecs_task_iam_role'] = ecs_task_iam_role
        self.props['task_execution_role'] = task_execution_role
        self.props['ecs_cluster'] = ecs_cluster
        self.props['ecs_default_cloud_map_namespace'] = ecs_default_cloud_map_namespace
        self.props['ecs_security_group'] = ecs_security_group
        self.props['sel_hub_sg'] = sel_hub_sg
        self.props['sel_node_sg'] = sel_node_sg
        self.props['ec2_public_lb_sg'] = ec2_public_lb_sg
        self.props['elbv2_public_lb'] = elbv2_public_lb
        self.props['elbv2_web_listener'] = elbv2_web_listener
        self.props['elbv2_api_listener'] = elbv2_api_listener
        self.props['elbv2_flower_listener'] = elbv2_flower_listener
        self.props['elbv2_selenium_hub_listener'] = elbv2_selenium_hub_listener
        self.props['elbv2_selenium_bus_publish_listener'] = elbv2_selenium_bus_publish_listener
        self.props['elbv2_selenium_bus_subscribe_listener'] = elbv2_selenium_bus_subscribe_listener
        self.props['sqs_queue_url'] = sqs_queue_url
        self.props['redis_url'] = redis_url
        self.props['lambda_get_redis_queue_message_count'] = lambda_get_redis_queue_message_count
        self.props['celery_worker_log_group_arn'] = celery_worker_batch_log_group_arn
        self.props['selenium_log_group_arn'] = selenium_log_group_arn
        self.props['api_base_url'] = api_base_url
        self.props['selenium_node_max_instances'] = selenium_node_max_instances
        self.props['selenium_node_max_sessions'] = selenium_node_max_sessions
        self.props['min_instances'] = min_instances
        self.props['max_instances'] = max_instances
        self.props['selenium_version'] = selenium_version
        self.props['api_port'] = api_port
        self.props['streamlit_port'] = streamlit_port
        self.props['celery_flower_port'] = celery_flower_port
        self.props['redis_cluster_id'] = redis_cluster_id
        self.props['redis_port'] = redis_port
        self.props['mysql_port'] = mysql_port
        self.props['selenium_hub_port'] = selenium_hub_port
        self.props['selenium_bus_publish_port'] = selenium_bus_publish_port
        self.props['selenium_bus_subscribe_port'] = selenium_bus_subscribe_port
        self.props['selenium_node_port'] = selenium_node_port
        self.props['open_api_key'] = open_api_key
        self.props['streamlit_email'] = streamlit_email
        self.props['li_client_id'] = li_client_id
        self.props['li_client_secret'] = li_client_secret
        self.props['li_state_salt'] = li_state_salt
        self.props['li_api_version'] = li_api_version
        self.props['pexels_api_key'] = pexels_api_key
        self.props['hf_token'] = hf_token
        self.props['replicate_api_token'] = replicate_api_token
        self.props['runwayml_api_secret'] = runwayml_api_secret
        self.props['tz'] = tz
        self.props['purge_tasks'] = purge_tasks
        self.props['clear_selenium_sessions'] = clear_selenium_sessions
        self.props['device_farm_project_arn'] = device_farm_project_arn
        self.props['test_grid_project_arn'] = test_grid_project_arn

    def get(self, key):
        return self.props.get(key)

    def set(self, key, value):
        self.props[key] = value

    @property
    def env(self) -> Environment:
        return self.get('env')

    @property
    def ec2_vpc(self) -> ec2.Vpc:
        return self.get('ec2_vpc')

    @property
    def ecr_docker_asset(self) -> DockerImageAsset:
        return self.get('ecr_docker_asset')

    @property
    def rds_db_instance_endpoint_address(self) -> str:
        return self.get('rds_db_instance_endpoint_address')

    @property
    def ssm_myql_secret_name(self) -> str:
        return self.get('ssm_myql_secret_name')

    @property
    def efs_file_system(self) -> efs.FileSystem:
        return self.get('efs_file_system')

    @property
    def efs_access_point(self) -> efs.AccessPoint:
        return self.get('efs_access_point')

    @property
    def efs_volume_configuration(self) -> ecs.EfsVolumeConfiguration:
        return self.get('efs_volume_configuration')

    @property
    def efs_task_role(self) -> iam.Role:
        return self.get('efs_task_role')

    @property
    def efs_volume_name(self) -> str:
        return self.get('efs_volume_name')

    @property
    def efs_app_assets_path(self) -> str:
        return self.get('efs_app_assets_path')

    @property
    def ecs_asset_mount_point(self) -> ecs.MountPoint:
        return self.get('ecs_asset_mount_point')

    @property
    def ecs_celery_flower_data_mount_point(self) -> ecs.MountPoint:
        return self.get('ecs_celery_flower_data_mount_point')

    @property
    def ecs_task_iam_role(self) -> iam.Role:
        return self.get('ecs_task_iam_role')

    @property
    def task_execution_role(self) -> iam.Role:
        return self.get('task_execution_role')

    @property
    def ecs_cluster(self) -> ecs.Cluster:
        return self.get('ecs_cluster')

    @property
    def ecs_default_cloud_map_namespace(self) -> INamespace:
        return self.get('ecs_default_cloud_map_namespace')

    @property
    def ecs_security_group(self) -> ec2.SecurityGroup:
        return self.get('ecs_security_group')

    @property
    def sel_hub_sg(self) -> ec2.SecurityGroup:
        return self.get('sel_hub_sg')

    @property
    def sel_node_sg(self) -> ec2.SecurityGroup:
        return self.get('sel_node_sg')

    @property
    def public_lb_sg(self) -> ec2.SecurityGroup:
        return self.get('public_lb_sg')

    @property
    def ec2_public_lb_sg(self) -> ec2.SecurityGroup:
        return self.get('ec2_public_lb_sg')

    @property
    def elbv2_public_lb(self) -> elbv2.ApplicationLoadBalancer:
        return self.get('elbv2_public_lb')

    # Store the full ARN
    @property
    def elbv2_public_lb_arn(self) -> str:
        return f"{self.elbv2_public_lb.load_balancer_arn}"

    # Get just the name
    @property
    def elbv2_public_lb_name(self) -> str:
        # return self.elbv2_public_lb_arn.split('//')[2] if self.elbv2_public_lb_arn else None
        match = re.search(r'\/([^\/]+)\/[^\/]+$', self.elbv2_public_lb_arn)
        return match.group(1) if match else None

    # Get just the ID
    @property
    def elbv2_public_lb_id(self) -> str:
        # return self.elbv2_public_lb_arn.split('//')[-1] if self.elbv2_public_lb_arn else None
        match = re.search(r'\/([^\/]+)$', self.elbv2_public_lb_arn)
        return match.group(1) if match else None

    @property
    def elbv2_web_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_web_listener')

    @property
    def elbv2_api_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_api_listener')

    @property
    def elbv2_flower_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_flower_listener')

    @property
    def elbv2_selenium_hub_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_selenium_hub_listener')

    @property
    def elbv2_selenium_bus_publish_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_selenium_bus_publish_listener')

    @property
    def elbv2_selenium_bus_subscribe_listener(self) -> elbv2.ApplicationListener:
        return self.get('elbv2_selenium_bus_subscribe_listener')

    @property
    def sqs_queue_url(self) -> str:
        return self.get('sqs_queue_url')

    @property
    def redis_cluster_id(self) -> str:
        return self.get('redis_cluster_id')

    @property
    def redis_url(self) -> str:
        return self.get('redis_url')

    @property
    def lambda_get_redis_queue_message_count(self) -> IFunction:
        return self.get('lambda_get_redis_queue_message_count')

    @property
    def celery_worker_batch_log_group_arn(self) -> str:
        return self.get('celery_worker_batch_log_group_arn')

    @property
    def selenium_log_group_arn(self) -> str:
        return self.get('selenium_log_group_arn')

    @property
    def api_base_url(self) -> str:
        return self.get('api_base_url')

    @property
    def selenium_node_max_instances(self) -> int:
        return self.get('selenium_node_max_instances')

    @property
    def selenium_node_max_sessions(self) -> int:
        return self.get('selenium_node_max_sessions')

    @property
    def min_instances(self) -> int:
        return self.get('min_instances')

    @property
    def max_instances(self) -> int:
        return self.get('max_instances')

    def __str__(self):
        return str(self.props)

    @property
    def selenium_version(self) -> str:
        return self.get('selenium_version')

    @property
    def api_port(self) -> int:
        return self.get('api_port')

    @property
    def streamlit_port(self) -> int:
        return self.get('streamlit_port')

    @property
    def celery_flower_port(self) -> int:
        return self.get('celery_flower_port')

    @property
    def redis_port(self) -> int:
        return self.get('redis_port')

    @property
    def mysql_port(self) -> int:
        return self.get('mysql_port')

    @property
    def selenium_hub_port(self) -> int:
        return self.get('selenium_hub_port')

    @property
    def selenium_bus_publish_port(self) -> int:
        return self.get('selenium_bus_publish_port')

    @property
    def selenium_bus_subscribe_port(self) -> int:
        return self.get('selenium_bus_subscribe_port')

    @property
    def selenium_node_port(self) -> int:
        return self.get('selenium_node_port')

    @property
    def open_api_key(self) -> str:
        return self.get('open_api_key')

    @property
    def streamlit_email(self) -> str:
        return self.get('streamlit_email')

    @property
    def li_client_id(self) -> str:
        return self.get('li_client_id')

    @property
    def li_client_secret(self) -> str:
        return self.get('li_client_secret')

    @property
    def li_redirect_url(self) -> str:
        return f"{self.api_base_url}:{self.api_port}/auth/linkedin/callback"

    @property
    def li_state_salt(self) -> str:
        return self.get('li_state_salt')

    @property
    def li_api_version(self) -> str:
        return self.get('li_api_version')

    @property
    def pexels_api_key(self) -> str:
        return self.get('pexels_api_key')

    @property
    def hf_token(self) -> str:
        return self.get('hf_token')

    @property
    def replicate_api_token(self) -> str:
        return self.get('replicate_api_token')

    @property
    def runwayml_api_secret(self) -> str:
        return self.get('runwayml_api_secret')

    @property
    def tz(self) -> str:
        return self.get('tz')

    @property
    def purge_tasks(self) -> bool:
        return self.get('purge_tasks')

    @property
    def clear_selenium_sessions(self) -> bool:
        return self.get('clear_selenium_sessions')

    @property
    def device_farm_project_arn(self) -> str:
        return self.get('device_farm_project_arn')

    @property
    def test_grid_project_arn(self) -> str:
        return self.get('test_grid_project_arn')
