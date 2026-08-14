# Self-Healing Selenium Grid with Celery Integration

> **Historical design note — not buildable as written (#973).** Every snippet below targets AWS
> ECS/Fargate through the CDK deploy path, which was deleted on 2026-08-11; `aws_ecs`,
> `aws_appautoscaling` and `aws_cloudwatch` are no longer dependencies of this repo. Docker Compose
> on the VPS is the only supported deploy. What ships today — the fixed Chrome session pool, the
> `SE_NODE_MAX_SESSIONS` invariant and the horizontal `docker-compose.grid.yml` path — is
> `docs/SELENIUM_GRID.md` and `docs/scaling-plan.md`. Kept for the self-healing/auto-scaling ideas,
> which still apply; read the AWS mechanics as pseudocode.

## Table of Contents
1. Overview
   - Purpose
   - Architecture
   - Key Components

2. Infrastructure Setup
   - ECS Cluster Configuration
   - Selenium Hub Service
   - Selenium Node Service
   - Network Configuration
   - Service Discovery

3. Application Components
   - Resilient Session Manager
   - Celery Task Base Class
   - Environment Configuration
   - Health Checks Implementation
   - Auto-scaling Configuration

4. Deployment Guide
   - Prerequisites
   - CDK Stack Setup
   - Environment Variables
   - Deployment Steps
   - Verification Steps

5. Testing Strategy
   - Unit Tests
   - Integration Tests
   - Load Tests
   - Failure Scenarios

6. Monitoring and Maintenance
   - CloudWatch Metrics
   - Logging Strategy
   - Alerting Setup
   - Common Issues and Solutions

7. Best Practices
   - Session Management
   - Error Handling
   - Scaling Considerations
   - Security Guidelines

8. Troubleshooting Guide
   - Common Issues
   - Debug Procedures
   - Recovery Steps
   - Support Matrix


```python
# In your selenium_stack.py
environment={
    # Existing configs from your file
    "SE_NODE_MAX_SESSIONS": str(props.selenium_node_max_sessions),
    "SE_NODE_OVERRIDE_MAX_SESSIONS": "false",
    "SE_VNC_NO_PASSWORD": "true",
    "SE_EVENT_BUS_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
    
    # Add these configurations for better resilience
    "SE_NODE_SESSION_TIMEOUT": "1200",  # 20 minutes
    "SE_SESSION_RETRY_INTERVAL": "15",
    "SE_START_XVFB": "true",
    "SE_DRAIN_AFTER_SESSION_COUNT": str(props.selenium_node_max_sessions * 10),  # Drain after X sessions
    "SE_GRID_ROUTER_HOST": f"selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}",
    "SE_GRID_HEARTBEAT_PERIOD": "10",
    "SE_NODE_GRID_URL": f"http://selenium_hub.{props.ecs_default_cloud_map_namespace.namespace_name}:4444",
}
```
Create a resilient Selenium session manager:

```python
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from tenacity import retry, stop_after_attempt, wait_exponential

class ResilientSeleniumManager:
    def __init__(self, hub_url, session_timeout=1200):
        self.hub_url = hub_url
        self.session_timeout = session_timeout
        self.driver = None
        
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10)
    )
    def create_driver(self):
        options = webdriver.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        
        capabilities = {
            "browserName": "chrome",
            "se:timeZone": "UTC",
            "se:noVnc": True,
            "se:vncEnabled": True,
            "se:sessionTimeout": str(self.session_timeout),
        }
        options.set_capability('se:options', capabilities)
        
        return webdriver.Remote(
            command_executor=self.hub_url,
            options=options
        )

    def __enter__(self):
        self.driver = self.create_driver()
        return self.driver

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass

```

Create a Celery task base class with retry logic

```python
from celery import Task
from requests.exceptions import RequestException
from selenium.common.exceptions import WebDriverException

class SeleniumTask(Task):
    abstract = True
    autoretry_for = (WebDriverException, RequestException)
    max_retries = 3
    retry_backoff = True
    retry_backoff_max = 600
    retry_jitter = True

    def __init__(self):
        self.driver = None

    def get_selenium_manager(self):
        return ResilientSeleniumManager(
            hub_url="http://selenium_hub:4444/wd/hub"
        )

    def __call__(self, *args, **kwargs):
        with self.get_selenium_manager() as driver:
            self.driver = driver
            return self.run(*args, **kwargs)
```

Implement your Celery task using the base class

```python
@celery.task(base=SeleniumTask, bind=True)
def process_with_selenium(self, url):
    try:
        self.driver.get(url)
        # Add explicit waits for better reliability
        wait = WebDriverWait(self.driver, 20)
        element = wait.until(
            EC.presence_of_element_located((By.ID, "some-id"))
        )
        # Your processing logic here
        return {"status": "success"}
        
    except Exception as exc:
        # Log the exception details
        logger.error(f"Task failed: {exc}")
        raise self.retry(exc=exc)
```

Add health checks to your ECS service:

```python
container = task_definition.add_container(
    "selenium-node",
    image=aws_ecs.ContainerImage.from_registry("selenium/node-chrome"),
    health_check=aws_ecs.HealthCheck(
        command=[
            "CMD-SHELL",
            "curl -f http://localhost:5556/status || exit 1"
        ],
        interval=Duration.seconds(30),
        timeout=Duration.seconds(10),
        retries=3,
        start_period=Duration.seconds(60)
    )
)
```

Implement proper autoscalling with cooldown periods

```python

scaling = selenium_node_service.auto_scale_task_count(
    aws_appautoscaling.EnableScalingProps(
        min_capacity=1,
        max_capacity=10
    )
)

scaling.scale_on_cpu_utilization(
    "CpuScaling",
    target_utilization_percent=70,
    scale_in_cooldown=Duration.seconds(300),  # 5 minutes
    scale_out_cooldown=Duration.seconds(60)   # 1 minute
)

# Add custom metric scaling based on queue length
scaling.scale_on_metric(
    "QueueScaling",
    aws_appautoscaling.TargetTrackingScalingPolicyProps(
        target_value=2.0,  # Maintain 2 tasks per active session
        scale_in_cooldown=Duration.seconds(300),
        scale_out_cooldown=Duration.seconds(60),
        custom_metric=aws_cloudwatch.Metric(
            namespace="SeleniumGrid",
            metric_name="ActiveSessions",
            statistic="Average",
            period=Duration.seconds(60)
        )
    )
)

```     

This setup provides:

Automatic retry logic for failed tasks

Graceful handling of node termination

Session timeout management

Health monitoring

Proper scaling behavior with cooldown periods

Self-healing capabilities through ECS service auto-recovery

The combination of these components creates a robust system that can:

Recover from temporary failures

Handle node termination gracefully

Maintain session state during scaling events

Automatically retry failed operations

Properly clean up resources

Scale based on actual usage patterns

Remember to adjust the timeout values, retry attempts, and scaling parameters based on your specific workload requirements.


