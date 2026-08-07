import os

import boto3


def lambda_handler(event, context):
    ecs = boto3.client('ecs')
    cluster_arn = os.environ['CLUSTER_ARN']
    streamlit_service_arn = os.environ['STREAMLIT_SERVICE_ARN']
    flower_service_arn = os.environ['FLOWER_SERVICE_ARN']

    # Update Streamlit service desired count to 1
    ecs.update_service(
        cluster=cluster_arn,
        service=streamlit_service_arn,
        desired_count=1
    )

    # Update Flower service desired count to 1
    ecs.update_service(
        cluster=cluster_arn,
        service=flower_service_arn,
        desired_count=1
    )

    return {
        'statusCode': 200,
        'body': 'Services started successfully'
    }