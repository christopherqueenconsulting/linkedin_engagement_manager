from datetime import datetime

import cdk_ecr_deployment as ecrdeploy
from aws_cdk import NestedStack, RemovalPolicy, aws_ecr as ecr, aws_ecr_assets as ecr_assets
from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform
from constructs import Construct

from cqc_lem import build_dir


class EcrStack(NestedStack):

    def __init__(self, scope: Construct, id: str,
                 **kwargs, ) -> None:
        super().__init__(scope, id, **kwargs)

        # Create ecr repository that will host the docker image for our services
        repository = ecr.Repository(self, "Repository",
                                    repository_name="cqc-lem",
                                    removal_policy=RemovalPolicy.DESTROY,
                                    empty_on_delete=True,
                                    lifecycle_rules=[
                                        ecr.LifecycleRule(
                                            max_image_count=5,  # Keep only the 5 most recent images
                                            description="Cleanup old images"
                                        )
                                    ]
                                    )

        # Create timestamp to add and create unique asset_name
        timestamp = (datetime.now()).strftime("%Y%m%d%H%M%S")
        asset_name = f"cqc-lem-docker-image-{timestamp}"

        # Build the Docker images
        docker_asset = DockerImageAsset(self, "dockerAsset",
                                        asset_name=asset_name,
                                        directory=build_dir,
                                        file='./compose/local/Dockerfile',
                                        exclude=[
                                            '**/.*/*',
                                            '**/_CL/**/*',
                                            '**/docs/**/*',
                                            '**/logs/**/*',
                                            '**/src/cqc_lem/aws/**/*',
                                            '**/src/cqc_lem/assets/**/*',
                                            '**/compose/local/linkedinpreview/**/*',
                                            '**/test/**/*',
                                            # Additional recommended exclusions
                                            '**/__pycache__',
                                            '**/*.pyc',
                                            '**/*.pyo',
                                            '**/*.pyd',
                                            '.pytest_cache',
                                            '.coverage',
                                            '.git',
                                            '.gitignore',
                                            '.env',
                                            '.venv',
                                            'venv',
                                            'ENV',
                                            'dist',
                                            'build',
                                            '**/*.egg-info',
                                            '.idea',
                                            '.vscode',
                                            'docker-compose*.yml',
                                            'Dockerfile',
                                            '.dockerignore',
                                            'htmlcov',
                                            '**/*.log',
                                            '**/*.swp',
                                            '**/*.swo'
                                        ],
                                        build_args={
                                            # Make sure the dev dependencies are not installed
                                            "INSTALL_DEV_DEPS":"false"
                                        },
                                        platform=Platform.LINUX_AMD64,
                                        invalidation=ecr_assets.DockerImageAssetInvalidationOptions(
                                            build_args=False
                                        )
                                        )

        # Deploying images to ECR
        ecrdeploy.ECRDeployment(self, "DeployImage",
                                src=ecrdeploy.DockerImageName(docker_asset.image_uri),
                                dest=ecrdeploy.DockerImageName(
                                    f"{repository.repository_uri}:latest"),
                                memory_limit=2048

                                )

        # Exporting values to be used in other stacks
        self.docker_asset = docker_asset
