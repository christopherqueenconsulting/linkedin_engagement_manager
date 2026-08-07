import os

import redis


def lambda_handler(event, context):
    redis_url = os.getenv('REDIS_URL')
    redis_port = os.getenv('REDIS_PORT')
    redis_db = os.getenv('REDIS_DB')
    celery_queue_name = os.getenv('CELERY_QUEUE_NAME')

    #print(f"Redis URL: {redis_url}")
    #print(f"Redis Port: {redis_port}")
    #print(f"Redis DB: {redis_db}")
    #print(f"Celery Queue Name: {celery_queue_name}")

    try:
        r = redis.Redis(host=redis_url, port=redis_port, db=redis_db)

        message_count = r.llen(celery_queue_name)  # This is the queue name

        # Close redis connection
        r.close()

        return {
            'statusCode': 200,
            'message_count': message_count
        }

    except Exception as e:
        print(f"Error connecting to Redis: {e}")
        return {
            'statusCode': 500,
            'body': 'Failed to connect to Redis',
            'message_count': 0
        }


if __name__ == '__main__':
    response = lambda_handler({}, "Testing Function")
    print(response['body'])
