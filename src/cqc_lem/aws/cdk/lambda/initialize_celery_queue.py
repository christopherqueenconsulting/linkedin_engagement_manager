
import redis


def handler(event, context):
    redis_url = event['ResourceProperties']['RedisUrl']
    r = redis.Redis.from_url(redis_url)

    # Initialize the Celery queue
    r.lpush('celery', 'initialize')

    return {
        'Status': 'SUCCESS',
        'PhysicalResourceId': 'InitializeCeleryQueue',
        'Data': {}
    }