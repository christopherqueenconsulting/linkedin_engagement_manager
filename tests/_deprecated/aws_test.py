from cqc_lem.app.aws_test_celery_task import test_task


def run_test():
    # Send 5 tasks with different sleep times
    for i in range(1):
        print(f"Sending task {i}")
        test_task.delay(i)


if __name__ == "__main__":
    run_test()