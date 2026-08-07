#!/usr/bin/env python3

import aws_cdk as cdk

from cqc_lem.aws.cdk.ecs.fargate_service.api_stack import APIStack
from cqc_lem.aws.cdk.ecs.fargate_service.celery_beat_stack import CeleryBeatStack
from cqc_lem.aws.cdk.ecs.fargate_service.celery_flower_stack import CeleryFlowerStack
from cqc_lem.aws.cdk.ecs.fargate_service.celery_worker_stack import CeleryWorkerStack
from cqc_lem.aws.cdk.ecs.fargate_service.web_stack import WebStack
from cqc_lem.aws.cdk.main_stack import MainStack
from cqc_lem.aws.cdk.shared_stack_props import SharedStackProps
from cqc_lem.aws.util import get_cdk_env
from cqc_lem.utilities.env_constants import (
                       HF_TOKEN,
                       LI_API_VERSION,
                       LI_CLIENT_ID,
                       LI_CLIENT_SECRET,
                       LI_STATE_SALT,
                       OPENAI_API_KEY,
                       PEXELS_API_KEY,
                       REPLICATE_API_TOKEN,
                       RUNWAYML_API_SECRET,
                       STREAMLIT_EMAIL,
                       TZ,
)

app = cdk.App()
env = get_cdk_env()
props = SharedStackProps(env=env)

# Set properties from environment variables
props.set("open_api_key", OPENAI_API_KEY)
props.set("streamlit_email", STREAMLIT_EMAIL)
props.set("li_client_id", LI_CLIENT_ID)
props.set("li_client_secret", LI_CLIENT_SECRET)
props.set("li_state_salt", LI_STATE_SALT)
props.set("li_api_version", LI_API_VERSION)
props.set("pexels_api_key", PEXELS_API_KEY)
props.set("hf_token", HF_TOKEN)
props.set("replicate_api_token", REPLICATE_API_TOKEN)
props.set("runwayml_api_secret", RUNWAYML_API_SECRET)
props.set("tz", TZ)
# TODO: Uncomment below
#props.set("purge_tasks", PURGE_TASKS)
#props.set("clear_selenium_sessions", CLEAR_SELENIUM_SESSIONS)

main_stack = MainStack(app, "CQC-LEM",
                       props=props,
                       env=env)

web_stack = WebStack(app, "WebStack",
                     env=env,
                     props=main_stack.outputs
                     )

# Add dependencies to ensure correct order
web_stack.add_dependency(main_stack)

api_stack = APIStack(app, "APIStack",
                     env=env,
                     props=main_stack.outputs
                     )

# Add dependencies to ensure correct order
api_stack.add_dependency(main_stack)

# Use Device Farm Stack instead of Selenium Stack
#device_farm_stack = DeviceFarmStack(app, "DeviceFarmStack",
#                                    env=env,
#                                    props=main_stack.outputs
#                                    )
# Add dependencies to ensure correct order
# device_farm_stack.add_dependency(main_stack)

'''
selenium_stack = SeleniumStack(app, "SeleniumStack",
                               env=env,
                               props=main_stack.outputs,
                               hub_cpu=512,
                               hub_memory_limit=1024,
                               node_cpu=256,  # TODO: Refine these Selenium Node CPU values
                               node_memory_limit=512,  # TODO: Refine these Selenium Node values

                               )
# Add dependencies to ensure correct order
selenium_stack.add_dependency(main_stack)
'''

celery_worker_stack = CeleryWorkerStack(app, "CeleryWorkerStack",
                                        env=env,
                                        props=main_stack.outputs
                                        # props=device_farm_stack.outputs
                                        # props=selenium_stack.outputs
                                        )

# Add dependencies to ensure correct order
celery_worker_stack.add_dependency(main_stack)
# celery_worker_stack.add_dependency(selenium_stack)
# celery_worker_stack.add_dependency(device_farm_stack)

celery_flower_stack = CeleryFlowerStack(app, "CeleryFlowerStack", env=env,
                                        props=main_stack.outputs
                                        )

# Add dependencies to ensure correct order
celery_flower_stack.add_dependency(main_stack)

celery_beat_stack = CeleryBeatStack(app, "CeleryBeatStack", env=env,
                                    props=main_stack.outputs
                                    # props=device_farm_stack.outputs
                                    # props=selenium_stack.outputs
                                    )

# Add dependencies to ensure correct order
celery_beat_stack.add_dependency(main_stack)
# celery_beat_stack.add_dependency(selenium_stack)
# celery_beat_stack.add_dependency(device_farm_stack)

app.synth()
