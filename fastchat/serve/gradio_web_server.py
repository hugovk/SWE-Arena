"""
The gradio demo server for chatting with a single model.
"""

import argparse
from collections import defaultdict
import hashlib
import json5
import os
import random
import time
from typing import List
from gradio_sandboxcomponent import SandboxComponent

import gradio as gr
import requests

from fastchat.constants import (
    WORKER_API_TIMEOUT,
    ErrorCode,
    MODERATION_MSG,
    CONVERSATION_LIMIT_MSG,
    RATE_LIMIT_MSG,
    SERVER_ERROR_MSG,
    INPUT_CHAR_LEN_LIMIT,
    CONVERSATION_TURN_LIMIT,
    SESSION_EXPIRATION_TIME,
    SURVEY_LINK,
)
from fastchat.conversation import Conversation
from fastchat.model.model_registry import get_model_info, model_info
from fastchat.serve.chat_state import LOG_DIR, ModelChatState, save_log_to_local
from fastchat.serve.api_provider import get_api_provider_stream_iter
from fastchat.serve.gradio_global_state import Context
from fastchat.serve.remote_logger import get_remote_logger
from fastchat.serve.sandbox.sandbox_state import ChatbotSandboxState
from fastchat.serve.sandbox.code_runner import SandboxGradioSandboxComponents, SandboxEnvironment, DEFAULT_SANDBOX_INSTRUCTIONS, RUN_CODE_BUTTON_HTML, SUPPORTED_SANDBOX_ENVIRONMENTS, create_chatbot_sandbox_state, on_click_code_message_run, on_edit_code, reset_sandbox_state, set_sandbox_state_ids, update_sandbox_config, update_sandbox_state_system_prompt, update_visibility_for_single_model, on_edit_dependency
from fastchat.serve.sandbox.sandbox_telemetry import log_sandbox_telemetry_gradio_fn, save_conv_log_to_azure_storage
from fastchat.utils import (
    build_logger,
    get_window_url_params_js,
    get_window_url_params_with_tos_js,
    moderation_filter,
    parse_gradio_auth_creds,
    load_image,
)
from fastchat.serve.sandbox.code_analyzer import SandboxEnvironment

logger = build_logger("gradio_web_server", "gradio_web_server.log")

headers = {"User-Agent": "FastChat Client"}

no_change_btn = gr.Button()
enable_btn = gr.Button(interactive=True, visible=True)
disable_btn = gr.Button(interactive=False)
invisible_btn = gr.Button(interactive=False, visible=False)
enable_text = gr.Textbox(
    interactive=True, visible=True, placeholder="👉 Enter your prompt and press ENTER"
)
disable_text = gr.Textbox(
    interactive=False,
    visible=True,
    placeholder='Press "🎲 New Round" to start over👇 (Note: Your vote shapes the leaderboard, please vote RESPONSIBLY!)',
)

controller_url = None
enable_moderation = False
use_remote_storage = False

acknowledgment_md = """

## Found an Issue?
Please report any bugs or issues to the [GitHub](https://github.com/BigComputer-Project/SWE-Arena) or join our [Discord](https://discord.gg/auCweVA9hB).

## Terms of Service

Users are required to agree to the following terms before using the service:

The service is a research preview. It only provides limited safety measures and may generate offensive content.
It must not be used for any illegal, harmful, violent, racist, or sexual purposes.
Please do not upload any private information.
The service collects user data, including dialogue (text and images), editing history, and interface interaction data, and reserves the right to distribute it under a Creative Commons Attribution (CC-BY) or similar license.

## Acknowledgments

SWE Arena extends [Chatbot Arena](https://lmarena.ai/?arena) with powerful code execution capabilities, enabling direct evaluation of LLM-generated programs across a wide range of outputs - from simple computations to complex visual interfaces.

We thank [E2B](https://e2b.dev/), [Hyperbolic](https://hyperbolic.xyz/), [Hugging Face](https://huggingface.co/), [Alibaba Qwen](https://qwenlm.ai/) and [CSIRO's Data61](http://data61.csiro.au) for their support and sponsorship:

<div class="sponsor-image-about">
    <img src="https://github.com/e2b-dev/E2B/blob/main/readme-assets/logo-circle.png?raw=true" alt="E2B">
    <img src="https://pbs.twimg.com/profile_images/1775708849707819008/1RRWsmmg_400x400.jpg" alt="Hyperbolic AI">
    <img src="https://huggingface.co/datasets/huggingface/brand-assets/resolve/main/hf-logo.png" alt="HuggingFace">
    <img src="https://qwenlm.ai/static/favicon.png" alt="Qwen">
    <img src="https://style.csiro.au/WP_Partners/assets/img/data61-logo.png" alt="Data61">
</div>
"""

# JSON file format of API-based models:
# {
#   "gpt-3.5-turbo": {
#     "model_name": "gpt-3.5-turbo",
#     "api_type": "openai",
#     "api_base": "https://api.openai.com/v1",
#     "api_key": "sk-******",
#     "anony_only": false
#   }
# }
#
#  - "api_type" can be one of the following: openai, anthropic, gemini, or mistral. For custom APIs, add a new type and implement it accordingly.
#  - "anony_only" indicates whether to display this model in anonymous mode only.

api_endpoint_info = {}

def set_global_vars(
    controller_url_,
    enable_moderation_,
    use_remote_storage_,
):
    global controller_url, enable_moderation, use_remote_storage
    controller_url = controller_url_
    enable_moderation = enable_moderation_
    use_remote_storage = use_remote_storage_


def get_model_list(controller_url, register_api_endpoint_file, vision_arena: bool):
    global api_endpoint_info

    # Add models from the controller
    if controller_url:
        ret = requests.post(controller_url + "/refresh_all_workers")
        assert ret.status_code == 200

        if vision_arena:
            ret = requests.post(controller_url + "/list_multimodal_models")
            models = ret.json()["models"]
        else:
            ret = requests.post(controller_url + "/list_language_models")
            models = ret.json()["models"]
    else:
        models = []

    # Add models from the API providers
    if register_api_endpoint_file:
        api_endpoint_info = json5.load(open(register_api_endpoint_file))
        for mdl, mdl_dict in api_endpoint_info.items():
            mdl_vision = mdl_dict.get("vision-arena", False)
            mdl_text = mdl_dict.get("text-arena", True)
            if vision_arena and mdl_vision:
                models.append(mdl)
            if not vision_arena and mdl_text:
                models.append(mdl)

    # Remove anonymous models
    models = list(set(models))
    visible_models = models.copy()
    for mdl in models:
        if mdl not in api_endpoint_info:
            continue
        mdl_dict = api_endpoint_info[mdl]
        if mdl_dict["anony_only"]:
            visible_models.remove(mdl)

    # Sort models and add descriptions
    priority = {k: f"___{i:03d}" for i, k in enumerate(model_info)}
    models.sort(key=lambda x: priority.get(x, x))
    visible_models.sort(key=lambda x: priority.get(x, x))
    logger.info(f"All models: {models}")
    logger.info(f"Visible models: {visible_models}")
    return visible_models, models


def load_demo_single(context: Context, query_params):
    # default to text models
    models = context.text_models

    selected_model = models[0] if len(models) > 0 else ""
    if "model" in query_params:
        model = query_params["model"]
        if model in models:
            selected_model = model

    all_models = context.models

    dropdown_update = gr.Dropdown(
        choices=all_models, value=selected_model, visible=True
    )
    state = None
    return [state, dropdown_update]


def load_demo(url_params, request: gr.Request):
    global models

    ip = get_ip(request)
    logger.info(f"load_demo. ip: {ip}. params: {url_params}")

    if args.model_list_mode == "reload":
        models, all_models = get_model_list(
            controller_url, args.register_api_endpoint_file, vision_arena=False
        )

    return load_demo_single(models, url_params)


def vote_last_response(
    state: ModelChatState,
    vote_type,
    model_selector: str,
    request: gr.Request
):
    local_filepath = state.get_conv_log_filepath(LOG_DIR)
    log_data = state.generate_vote_record(vote_type, get_ip(request))
    get_remote_logger().log(log_data)
    save_log_to_local(log_data, local_filepath)
    # save_conv_log_to_azure_storage(local_filepath.lstrip(LOCAL_LOG_DIR), log_data)


def upvote_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"upvote. ip: {ip}")
    vote_last_response(state, "upvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def downvote_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"downvote. ip: {ip}")
    vote_last_response(state, "downvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def flag_last_response(state, model_selector, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"flag. ip: {ip}")
    vote_last_response(state, "flag", model_selector, request)
    return ("",) + (disable_btn,) * 3


def regenerate(state, request: gr.Request):
    ip = get_ip(request)
    logger.info(f"regenerate. ip: {ip}")
    if not state.regen_support:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "", None) + (no_change_btn,) * 5
    state.conv.update_last_message(None)
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 5


def clear_history(sandbox_state: ChatbotSandboxState, request: gr.Request):
    '''
    Clear the conversation history and reset the state.
    '''
    # ip = get_ip(request)
    # logger.info(f"clear_history. ip: {ip}")

    # reset chatbot state
    state = None

    # reset sandbox state
    sandbox_state = reset_sandbox_state(sandbox_state)

    return (
        ("Auto",)
        + (state, [], "")
        + (disable_btn,) * 5 # disable buttons
        + (sandbox_state,)
    )


def clear_sandbox_components(*components):
    updates = []
    for idx, component in enumerate(components):
        if idx in [3, 7]:
            updates.append(gr.update(value=[['', '', '']], visible=False))
        else:
            updates.append(gr.update(value="", visible=False))
    return updates

def get_ip(request: gr.Request):
    if request is None:
        return None
    if "cf-connecting-ip" in request.headers:
        ip = request.headers["cf-connecting-ip"]
    elif "x-forwarded-for" in request.headers:
        ip = request.headers["x-forwarded-for"]
        if "," in ip:
            ip = ip.split(",")[0]
    else:
        ip = request.client.host
    return ip

def set_chat_system_messages(state, sandbox_state, model_selector):
    '''
    Add sandbox instructions to the system message.
    '''
    # if sandbox_state is None or sandbox_state['enable_sandbox'] is False or sandbox_state["enabled_round"] > 0:
    #     pass
    # else:
    # if state is None:
    #     state = State(model_selector)
    if state is None:
        print("Skipping set_chat_system_messages as state is None")
        return gr.skip(), gr.skip(), gr.skip()

    # sandbox_state['enabled_round'] += 1 # avoid dup
    environment_instruction = sandbox_state['sandbox_instruction']
    current_system_message = state.conv.get_system_message(state.is_vision)
    state.conv.set_system_message(environment_instruction) # update system message here
    return state, state.to_gradio_chatbot(), environment_instruction

def update_system_prompt(system_prompt, sandbox_state):
    '''
    TODO: Rewrite the code
    '''
    if sandbox_state['enabled_round'] == 0:
        sandbox_state['sandbox_instruction'] = system_prompt
    return sandbox_state

def add_text(
    state,
    model_selector: str,
    sandbox_state: ChatbotSandboxState,
    text: str,
    request: gr.Request
):
    ip = get_ip(request)
    logger.info(f"add_text. ip: {ip}. len: {len(text)}")

    # init chatbot state if not exist
    if state is None:
        state = ModelChatState(
            model_name=model_selector,
            chat_mode="direct",
            is_vision=False,
            chat_session_id=None,
        )
        set_sandbox_state_ids(
            sandbox_state=sandbox_state,
            conv_id=state.conv_id,
            chat_session_id=state.chat_session_id
        )

    if len(text) <= 0:
        state.skip_next = True
        return (
            (state, state.to_gradio_chatbot(), "")
            + (no_change_btn,) * sandbox_state["btn_list_length"]
        )

    all_conv_text = state.conv.get_prompt()
    all_conv_text = all_conv_text[-2000:] + "\nuser: " + text
    flagged = moderation_filter(all_conv_text, [state.model_name])
    # flagged = moderation_filter(text, [state.model_name])
    if flagged:
        logger.info(f"violate moderation. ip: {ip}. text: {text}")
        # overwrite the original text
        text = MODERATION_MSG

    if (len(state.conv.messages) - state.conv.offset) // 2 >= CONVERSATION_TURN_LIMIT:
        logger.info(f"conversation turn limit. ip: {ip}. text: {text}")
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), CONVERSATION_LIMIT_MSG) + (
            no_change_btn,
        ) * sandbox_state["btn_list_length"]

    text = text[:INPUT_CHAR_LEN_LIMIT]  # Hard cut-off
    state.conv.append_message(state.conv.roles[0], text)
    state.conv.append_message(state.conv.roles[1], None)

    sandbox_state['enabled_round'] += 1 
    return (
        state,
        state.to_gradio_chatbot(),
        ""
    ) + (disable_btn,) * sandbox_state["btn_list_length"]


def model_worker_stream_iter(
    conv,
    model_name,
    worker_addr,
    prompt,
    temperature,
    repetition_penalty,
    top_p,
    max_new_tokens,
    images,
):
    # Make requests
    gen_params = {
        "model": model_name,
        "prompt": prompt,
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "max_new_tokens": max_new_tokens,
        "stop": conv.stop_str,
        "stop_token_ids": conv.stop_token_ids,
        "echo": False,
    }

    logger.info(f"==== request ====\n{gen_params}")

    if len(images) > 0:
        gen_params["images"] = images

    # Stream output
    response = requests.post(
        worker_addr + "/worker_generate_stream",
        headers=headers,
        json=gen_params,
        stream=True,
        timeout=WORKER_API_TIMEOUT,
    )
    for chunk in response.iter_lines(decode_unicode=False, delimiter=b"\0"):
        if chunk:
            data = json5.loads(chunk.decode())
            yield data


def is_limit_reached(model_name, ip):
    # Disable limit check for now
    # monitor_url = "http://localhost:9090"
    # try:
    #     ret = requests.get(
    #         f"{monitor_url}/is_limit_reached?model={model_name}&user_id={ip}", timeout=1
    #     )
    #     obj = ret.json()
    #     return obj
    # except Exception as e:
    #     logger.info(f"monitor error: {e}")
    #     return None
    return None


def bot_response(
    state: ModelChatState,
    temperature,
    top_p,
    max_new_tokens,
    sandbox_state: ChatbotSandboxState,
    request: gr.Request,
    apply_rate_limit=True,
    use_recommended_config=False,
):
    '''
    The main function for generating responses from the model.
    '''
    if request:
        ip = get_ip(request)
        logger.info(f"bot_response. ip: {ip}")

    start_tstamp = time.time()
    temperature = float(temperature)
    top_p = float(top_p)
    max_new_tokens = int(max_new_tokens)

    if state is None:
        yield (None, None) + (no_change_btn,) * sandbox_state["btn_list_length"]
        return

    if state.skip_next:
        # This generate call is skipped due to invalid inputs
        state.skip_next = False
        yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * sandbox_state["btn_list_length"]
        return

    if apply_rate_limit:
        ret = is_limit_reached(state.model_name, ip)
        if ret is not None and ret["is_limit_reached"]:
            error_msg = RATE_LIMIT_MSG + "\n\n" + ret["reason"]
            logger.info(f"rate limit reached. ip: {ip}. error_msg: {ret['reason']}")
            state.conv.update_last_message(error_msg)
            yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * sandbox_state["btn_list_length"]
            return

    conv: Conversation = state.conv
    model_name: str = state.model_name

    model_api_dict = (
        api_endpoint_info[model_name] if model_name in api_endpoint_info else None
    )
    images = conv.get_images()

    if model_api_dict is None:
        # if not API-based model, use worker
        # Query worker address
        ret = requests.post(
            controller_url + "/get_worker_address", json={"model": model_name}
        )
        worker_addr = ret.json()["address"]
        logger.info(f"model_name: {model_name}, worker_addr: {worker_addr}")

        # No available worker
        if worker_addr == "":
            conv.update_last_message(SERVER_ERROR_MSG)
            yield (
                state,
                state.to_gradio_chatbot(),
                (disable_btn,) * sandbox_state["btn_list_length"],
            )
            return

        # Construct prompt.
        # We need to call it here, so it will not be affected by "▌".
        prompt = conv.get_prompt()
        # Set repetition_penalty
        if "t5" in model_name:
            repetition_penalty = 1.2
        else:
            repetition_penalty = 1.0

        stream_iter = model_worker_stream_iter(
            conv,
            model_name,
            worker_addr,
            prompt,
            temperature,
            repetition_penalty,
            top_p,
            max_new_tokens,
            images,
        )
    else:
        # Remove system prompt for API-based models unless specified
        # Code Sandbox needs system prompt
        custom_system_prompt = model_api_dict.get("custom_system_prompt", False) or sandbox_state['enable_sandbox']
        if not custom_system_prompt:
            conv.set_system_message("")

        if use_recommended_config:
            recommended_config = model_api_dict.get("recommended_config", None)
            if recommended_config is not None:
                temperature = recommended_config.get("temperature", temperature)
                top_p = recommended_config.get("top_p", top_p)
                max_new_tokens = recommended_config.get(
                    "max_new_tokens", max_new_tokens
                )

        stream_iter = get_api_provider_stream_iter(
            conv,
            model_name,
            model_api_dict,
            temperature,
            top_p,
            max_new_tokens,
            state,
        )

    html_code = ' <span class="cursor"></span> '

    # conv.update_last_message("▌")
    if conv is None:
        yield (state, None) + (no_change_btn,) * sandbox_state["btn_list_length"]
        return
    conv.update_last_message(html_code)
    yield (state, state.to_gradio_chatbot()) + (disable_btn,) * (sandbox_state["btn_list_length"])

    try:
        log_data = {"text": ""}
        for i, log_data in enumerate(stream_iter):
            if log_data["error_code"] == 0:
                output = log_data["text"].strip()
                conv.update_last_message(output + "▌")
                # conv.update_last_message(output + html_code)
                yield (state, state.to_gradio_chatbot()) + (disable_btn,) * sandbox_state["btn_list_length"]
            else:
                output = log_data["text"] + f"\n\n(error_code: {log_data['error_code']})"
                conv.update_last_message(output)
                yield (state, state.to_gradio_chatbot()) + (disable_btn,) * (sandbox_state["btn_list_length"]-2) + (enable_btn, enable_btn)
                return
        output = log_data["text"].strip()
        conv.update_last_message(output)

        # [CODE SANDBOX] Add a "Run in Sandbox" button to the last message if code is detected
        if sandbox_state is not None and sandbox_state["enable_sandbox"]:
            last_message = conv.messages[-1]
            # Count occurrences of ``` to ensure code blocks are properly closed
            code_fence_count = last_message[1].count("```")
            if code_fence_count > 0 and code_fence_count % 2 == 0:  # Even number means closed code blocks
                if not last_message[1].endswith(RUN_CODE_BUTTON_HTML):
                    last_message[1] += "\n\n\n" + RUN_CODE_BUTTON_HTML

        yield (state, state.to_gradio_chatbot()) + (enable_btn,) * sandbox_state["btn_list_length"]
    except requests.exceptions.RequestException as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_REQUEST_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (enable_btn,) * sandbox_state["btn_list_length"]
        return
    except Exception as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_STREAM_UNKNOWN_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (enable_btn,) * sandbox_state["btn_list_length"]
        return

    logger.info(f"{output}")

    conv.save_new_images(
        has_csam_images=state.has_csam_image, use_remote_storage=use_remote_storage
    )

    # Log the conversation
    local_filepath = state.get_conv_log_filepath(LOG_DIR)
    log_data = state.generate_response_record(
        gen_params={
            "temperature": temperature,
            "top_p": top_p,
            "max_new_tokens": max_new_tokens,
        },
        start_ts=start_tstamp,
        end_ts=time.time(),
        ip=get_ip(request),
    )
    get_remote_logger().log(log_data)
    save_log_to_local(log_data, local_filepath)
    # save_conv_log_to_azure_storage(local_filepath.lstrip(LOCAL_LOG_DIR), log_data)


block_css = """
.prose {
    font-size: 105% !important;
}

#arena_leaderboard_dataframe table {
    font-size: 105%;
}
#full_leaderboard_dataframe table {
    font-size: 105%;
}

.tab-nav button {
    font-size: 18px;
}

.chatbot h1 {
    font-size: 130%;
}
.chatbot h2 {
    font-size: 120%;
}
.chatbot h3 {
    font-size: 110%;
}

#chatbot .prose {
    font-size: 90% !important;
}

.sponsor-image-about img {
    margin: 0 20px;
    margin-top: 20px;
    height: 40px;
    max-height: 100%;
    width: auto;
    float: left;
}

.cursor {
    display: inline-block;
    width: 7px;
    height: 1em;
    background-color: black;
    vertical-align: middle;
    animation: blink 1s infinite;
}

.dark .cursor {
    display: inline-block;
    width: 7px;
    height: 1em;
    background-color: white;
    vertical-align: middle;
    animation: blink 1s infinite;
}

@keyframes blink {
    0%, 50% { opacity: 1; }
    50.1%, 100% { opacity: 0; }
}

.app {
  max-width: 100% !important;
  padding-left: 5% !important;
  padding-right: 5% !important;
}

a {
    color: #1976D2; /* Your current link color, a shade of blue */
    text-decoration: none; /* Removes underline from links */
}
a:hover {
    color: #63A4FF; /* This can be any color you choose for hover */
    text-decoration: underline; /* Adds underline on hover */
}

.block {
  overflow-y: hidden !important;
}

#chatbot-section {
}

/* Highlight input area */
#user-input-region {
    border-style: solid !important;
    border-color: rgb(234, 88, 12) !important;
    border-width: 2px !important;
}
"""
'''
The css block is used to style the chatbot interface.
'''


# block_css = """
# #notice_markdown .prose {
#     font-size: 110% !important;
# }
# #notice_markdown th {
#     display: none;
# }
# #notice_markdown td {
#     padding-top: 6px;
#     padding-bottom: 6px;
# }
# #arena_leaderboard_dataframe table {
#     font-size: 110%;
# }
# #full_leaderboard_dataframe table {
#     font-size: 110%;
# }
# #model_description_markdown {
#     font-size: 110% !important;
# }
# #leaderboard_markdown .prose {
#     font-size: 110% !important;
# }
# #leaderboard_markdown td {
#     padding-top: 6px;
#     padding-bottom: 6px;
# }
# #leaderboard_dataframe td {
#     line-height: 0.1em;
# }
# #about_markdown .prose {
#     font-size: 110% !important;
# }
# #ack_markdown .prose {
#     font-size: 110% !important;
# }
# #chatbot .prose {
#     font-size: 105% !important;
# }
# .sponsor-image-about img {
#     margin: 0 20px;
#     margin-top: 20px;
#     height: 40px;
#     max-height: 100%;
#     width: auto;
#     float: left;
# }

# body {
#     --body-text-size: 14px;
# }

# .chatbot h1, h2, h3 {
#     margin-top: 8px; /* Adjust the value as needed */
#     margin-bottom: 0px; /* Adjust the value as needed */
#     padding-bottom: 0px;
# }

# .chatbot h1 {
#     font-size: 130%;
# }
# .chatbot h2 {
#     font-size: 120%;
# }
# .chatbot h3 {
#     font-size: 110%;
# }
# .chatbot p:not(:first-child) {
#     margin-top: 8px;
# }

# .typing {
#     display: inline-block;
# }

# """


def get_model_description_md(models):
    model_description_md = """
| | | |
| ---- | ---- | ---- |
"""
    ct = 0
    visited = set()
    for i, name in enumerate(models):
        minfo = get_model_info(name)
        if minfo.simple_name in visited:
            continue
        visited.add(minfo.simple_name)
        one_model_md = f"[{minfo.simple_name}]({minfo.link}): {minfo.description}"

        if ct % 3 == 0:
            model_description_md += "|"
        model_description_md += f" {one_model_md} |"
        if ct % 3 == 2:
            model_description_md += "\n"
        ct += 1
    return model_description_md

def build_about():
    about_markdown = """
# About Us

**SWE Arena** is an open-source platform that extends Chatbot Arena with powerful code execution capabilities, enabling direct evaluation of LLM-generated programs across a wide range of outputs - from simple computations to complex visual interfaces. Building upon the [FastChat project](https://github.com/lm-sys/FastChat), it provides a secure sandbox environment for running and evaluating AI-generated visual interfaces. We aim to establish transparent comparisons across different models while creating valuable datasets to advance research in code generation and UI development.

We welcome contributors to enhance the platform and encourage feedback to improve its functionality and usability.

## 💻 Support
- Documents (Markdown or Plain Text)
- Websites (single webpage)
- Scalable Vector Graphics (SVG) images
- Plots
- Tables
- Interactive React/Vue components
- PyGame
- Gradio/Streamlit interfaces
- Mermaid diagrams
- Python/JavaScrip/C/C++/Java/Rust/Golang Plain Code Runners
- And more coming soon!

## Technical Implementation
SWE Arena provides:
- **Code Execution**: Secure, sandboxed environment using E2B for executing code in supported languages
- **Programming Expert**: Choose from a variety of programming experts to help you with your code
- **System Prompt Customization**: Customize the system prompt to fit your needs
- **Dependency Management**: Automatic installation of dependencies via NPM and PIP
- **Code Editing**: On-the-fly code modification, testing, and re-execution
- **Dependency Editing**: On-the-fly dependency modification, testing, and re-execution
- **Interaction Tracking**: Comprehensive logging of user interactions with rendered UIs

## Open-source Contributors
- **Lead**: [Terry Yue Zhuo](https://terryyz.github.io/)
- **Contributors**: [Gary Liu](mailto:ksqod_code@pm.me), [Yuhan Cao](mailto:ycao0081@student.monash.edu), [Tianyang Liu](https://leolty.github.io/), [Kaixin Li](https://likaixin2000.github.io/), [Bo (Benjamin) Liu](https://benjamin-eecs.github.io/), [Jihan Yao](https://yaojh18.github.io/)
- **Advisors**: [Banghua Zhu](https://people.eecs.berkeley.edu/~banghua/), [Torsten Scholak](https://www.servicenow.com/research/author/torsten-scholak.html), [Atin Sood](https://atinsood.com/about/), [Julian McAuley](https://cseweb.ucsd.edu/~jmcauley/), [Zhenchang Xing](https://people.csiro.au/X/Z/Zhenchang-Xing/), [Xiaoning Du](https://xiaoningdu.github.io/)

## Contact
[Email](mailto:computer.intelligence.project@gmail.com) | [GitHub](https://github.com/BigComputer-Project/SWE-Arena) | [X](https://x.com/BigComProject) | [Discord](https://discord.gg/auCweVA9hB)

## Acknowledgments
We thank [E2B](https://e2b.dev/), [Hyperbolic](https://hyperbolic.xyz/), [Hugging Face](https://huggingface.co/), [Alibaba Qwen](https://qwenlm.ai/) and [CSIRO's Data61](http://data61.csiro.au) for their support and sponsorship:

<div class="sponsor-image-about">
    <img src="https://github.com/e2b-dev/E2B/blob/main/readme-assets/logo-circle.png?raw=true" alt="E2B">
    <img src="https://pbs.twimg.com/profile_images/1775708849707819008/1RRWsmmg_400x400.jpg" alt="Hyperbolic AI">
    <img src="https://huggingface.co/datasets/huggingface/brand-assets/resolve/main/hf-logo.png" alt="HuggingFace">
    <img src="https://qwenlm.ai/static/favicon.png" alt="Qwen">
    <img src="https://style.csiro.au/WP_Partners/assets/img/data61-logo.png" alt="Data61">
</div>
"""
    gr.Markdown(about_markdown, elem_id="about_markdown")


def build_single_model_ui(models, add_promotion_links=False):
    promotion = (
        f"""
## 👇 Chat Now!
"""
        if add_promotion_links
        else ""
    )

    state = gr.State()

    with gr.Group(elem_id="share-region-named"):
        with gr.Row(elem_id="model_selector_row"):
            model_selector = gr.Dropdown(
                choices=models,
                value=models[0] if len(models) > 0 else "",
                interactive=True,
                show_label=False,
                container=False,
            )
        with gr.Row():
            with gr.Accordion(
                f"🔍 Expand to see the descriptions of {len(models)} models",
                open=False,
            ):
                model_description_md = get_model_description_md(models)
                gr.Markdown(model_description_md, elem_id="model_description_markdown")

        chatbot = gr.Chatbot(
            elem_id="chatbot",
            label="Scroll down and start chatting",
            height=650,
            show_copy_button=True,
            latex_delimiters=[
                {"left": "$", "right": "$", "display": False},
                {"left": "$$", "right": "$$", "display": True},
                {"left": r"\(", "right": r"\)", "display": False},
                {"left": r"\[", "right": r"\]", "display": True},
            ],
        )

    # Sandbox state and components
    sandbox_state = None
    sandboxes_components: list[SandboxGradioSandboxComponents] = []
    sandbox_hidden_components = []

    with gr.Group():
        with gr.Row():
            sandbox_env_choice = gr.Dropdown(
                choices=SUPPORTED_SANDBOX_ENVIRONMENTS,
                label="Programming Expert",
                info="Predefinedsystem prompt",
                interactive=True,
                visible=True
            )

        with gr.Accordion(
            label="System Prompt (Click to edit!)",
            open=False
        ) as system_prompt_accordion:
                system_prompt_textbox = gr.Textbox(
                    value=DEFAULT_SANDBOX_INSTRUCTIONS[SandboxEnvironment.AUTO],
                    show_label=False,
                    lines=15,
                    placeholder="Edit system prompt here",
                    interactive=True,
                    elem_id="system_prompt_box"
                )

        with gr.Group():
            with gr.Accordion("Sandbox & Output", open=True, visible=True) as sandbox_instruction_accordion:
                with gr.Group(visible=True) as sandbox_group:
                    with gr.Column(visible=True, scale=1) as sandbox_column:
                        sandbox_state = gr.State(create_chatbot_sandbox_state(btn_list_length=5))
                        # Add containers for the sandbox output
                        sandbox_title = gr.Markdown(value=f"### Model Sandbox", visible=True)

                        with gr.Tab(label="Output", visible=True) as sandbox_output_tab:
                            sandbox_output = gr.Markdown(value="", visible=True)
                            sandbox_ui = SandboxComponent(
                                value=('', False, []),
                                show_label=True,
                                visible=True,
                            )

                        # log sandbox telemetry
                        sandbox_ui.change(
                            fn=log_sandbox_telemetry_gradio_fn,
                            inputs=[sandbox_state, sandbox_ui],
                        )

                        with gr.Tab(label="Code Editor", visible=True) as sandbox_code_tab:
                            sandbox_code = gr.Code(
                                value="",
                                interactive=True, # allow user edit
                                visible=False,
                                label='Sandbox Code',
                            )
                            with gr.Row():
                                sandbox_code_submit_btn = gr.Button(value="Apply Changes", visible=True, interactive=True, variant='primary', size='sm')

                        with gr.Tab(
                            label="Dependency Editor (Beta Mode)", visible=True
                        ) as sandbox_dependency_tab:
                            sandbox_dependency = gr.Dataframe(
                                headers=["Type", "Package", "Version"],
                                datatype=["str", "str", "str"],
                                col_count=(3, "fixed"),
                                interactive=True,
                                visible=False,
                                wrap=True,  # Enable text wrapping
                                max_height=200,
                                type="array",  # Add this line to fix the error
                            )
                            with gr.Row():
                                dependency_submit_btn = gr.Button(
                                    value="Apply Changes",
                                    visible=True,
                                    interactive=True,
                                    variant="primary",
                                    size="sm",
                                )

                        dependency_submit_btn.click(
                            fn=on_edit_dependency,
                            inputs=[
                                state,
                                sandbox_state,
                                sandbox_dependency,
                                sandbox_output,
                                sandbox_ui,
                                sandbox_code,
                            ],
                            outputs=[
                                sandbox_output,
                                sandbox_ui,
                                sandbox_code,
                                sandbox_dependency,
                            ],
                        )

                        sandbox_code_submit_btn.click(
                            fn=on_edit_code,
                            inputs=[
                                state,
                                sandbox_state,
                                sandbox_output,
                                sandbox_ui,
                                sandbox_code,
                                sandbox_dependency,
                            ],
                            outputs=[
                                sandbox_output,
                                sandbox_ui,
                                sandbox_code,
                                sandbox_dependency,
                            ],
                        )

                        sandboxes_components.append((
                            sandbox_output,
                            sandbox_ui,
                            sandbox_code,
                            sandbox_dependency,
                        ))

    with gr.Row(elem_id="user-input-region"):
        textbox = gr.Textbox(
            show_label=False,
            placeholder="👉 Enter your prompt and press ENTER",
            elem_id="input_box",
            label="Type your query",
        )
        send_btn = gr.Button(value="Send", variant="primary", scale=0)

    with gr.Row() as button_row:
        upvote_btn = gr.Button(value="👍  Upvote", interactive=False)
        downvote_btn = gr.Button(value="👎  Downvote", interactive=False)
        flag_btn = gr.Button(value="⚠️  Flag", interactive=False)
        regenerate_btn = gr.Button(value="🔄  Regenerate", interactive=False)
        clear_btn = gr.Button(value="🗑️  Clear history", interactive=False)

    with gr.Row() as examples_row:
        examples = gr.Examples(
            examples = [
                ["Write a Python script that uses the Gradio library to create a functional calculator. The calculator should support basic arithmetic operations: addition, subtraction, multiplication, and division. It should have two input fields for numbers and a dropdown menu to select the operation.", SandboxEnvironment.GRADIO],
                ["Write a Python script using the Streamlit library to create a web application for uploading and displaying files. The app should allow users to upload files of type .csv or .txt. If a .csv file is uploaded, display its contents as a table using Streamlit's st.dataframe() method. If a .txt file is uploaded, display its content as plain text.", SandboxEnvironment.STREAMLIT],
                ["Write a Python function to solve the Trapping Rain Water problem. The function should take a list of non-negative integers representing the height of bars in a histogram and return the total amount of water trapped between the bars after raining. Use an efficient algorithm with a time complexity of O(n).",SandboxEnvironment.PYTHON_RUNNER],
                ["Create a simple Pygame script for a game where the player controls a bouncing ball that changes direction when it collides with the edges of the window. Add functionality for the player to control a paddle using arrow keys, aiming to keep the ball from touching the bottom of the screen. Include basic collision detection and a scoring system that increases as the ball bounces off the paddle.", SandboxEnvironment.PYGAME],
                ["Create a financial management Dashboard using Vue.js, focusing on local data handling without APIs. Include features like a clean dashboard for tracking income and expenses, dynamic charts for visualizing finances, and a budget planner. Implement functionalities for adding, editing, and deleting transactions, as well as filtering by date or category. Ensure responsive design and smooth user interaction for an intuitive experience.", SandboxEnvironment.VUE],
                ["Create a Mermaid diagram to visualize a flowchart of a user login process. Include the following steps: User enters login credentials; Credentials are validated; If valid, the user is directed to the dashboard; If invalid, an error message is shown, and the user can retry or reset the password.",SandboxEnvironment.MERMAID],
            ],
            inputs = [textbox, sandbox_env_choice],
        )

    with gr.Accordion("Parameters", open=False) as parameter_row:
        temperature = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=0.7,
            step=0.1,
            interactive=True,
            label="Temperature",
        )
        top_p = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=1.0,
            step=0.1,
            interactive=True,
            label="Top P",
        )
        max_output_tokens = gr.Slider(
            minimum=16,
            maximum=4096,
            value=4096,
            step=64,
            interactive=True,
            label="Max output tokens",
        )

    if add_promotion_links:
        gr.Markdown(acknowledgment_md, elem_id="ack_markdown")

    # Register listeners
    btn_list = [upvote_btn, downvote_btn, flag_btn, regenerate_btn, clear_btn]
    upvote_btn.click(
        upvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    downvote_btn.click(
        downvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    flag_btn.click(
        flag_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    regenerate_btn.click(
        regenerate,
        state,
        [state, chatbot, textbox] + btn_list
    ).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens, sandbox_state],
        [state, chatbot] + btn_list,
    )
    clear_btn.click(
        clear_history,
        [sandbox_state], 
        [sandbox_env_choice]+[state, chatbot, textbox] + btn_list + [sandbox_state]
    ).then(
        # clear existing sandbox components
        clear_sandbox_components,
        inputs=[sandbox_output, sandbox_ui, sandbox_code],
        outputs=[sandbox_output, sandbox_ui, sandbox_code]
    ).then(
        # reset env and system prompt
        lambda: (
            gr.update(interactive=True, value=SandboxEnvironment.AUTO),
            gr.update(interactive=True, value=DEFAULT_SANDBOX_INSTRUCTIONS[SandboxEnvironment.AUTO])
        ),
        outputs=[sandbox_env_choice, system_prompt_textbox]
    ).then(
        lambda: gr.update(visible=True),
        inputs=None,
        outputs=examples_row
    )

    model_selector.change(
        clear_history, 
        [sandbox_state], 
        [sandbox_env_choice]+[state, chatbot, textbox] + btn_list + [sandbox_state]
    ).then(
        clear_sandbox_components,
        inputs=[sandbox_output, sandbox_ui, sandbox_code],
        outputs=[sandbox_output, sandbox_ui, sandbox_code]
    ).then(
        # enable env and prompt edit
        fn=lambda: [
            gr.update(interactive=True),
            gr.update(interactive=True)
        ],
        outputs=[system_prompt_textbox, sandbox_env_choice]
    )

    textbox.submit(
        update_system_prompt,
        inputs=[system_prompt_textbox, sandbox_state],
        outputs=[sandbox_state]
    ).then(
        add_text,
        [state, model_selector, sandbox_state, textbox],
        [state, chatbot, textbox] + btn_list,
    ).then(
        set_chat_system_messages,
        inputs=[state, sandbox_state, model_selector],
        outputs=[state, chatbot, system_prompt_textbox]
    ).then(
        # hide examples
        lambda: gr.update(visible=False),
        outputs=examples_row
    ).then(
        # disable env and prompt change
        lambda: (gr.update(interactive=False),) * 2,
        outputs=[sandbox_env_choice, system_prompt_textbox]
    ).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens, sandbox_state],
        [state, chatbot] + btn_list,
    )

    send_btn.click(
        update_system_prompt,
        inputs=[system_prompt_textbox, sandbox_state],
        outputs=[sandbox_state]
    ).then(
        add_text,
        [state, model_selector, sandbox_state, textbox],
        [state, chatbot, textbox] + btn_list,
    ).then(
        set_chat_system_messages,
        [state, sandbox_state, model_selector],
        [state, chatbot, system_prompt_textbox]
    ).then(
        # disable env and prompt change
        lambda: (gr.update(interactive=False),) * 2,
        outputs=[system_prompt_textbox, sandbox_env_choice]
    ).then(
        lambda: gr.update(visible=False),
        outputs=examples_row
    ).then(
        bot_response,
        [state, temperature, top_p, max_output_tokens, sandbox_state],
        [state, chatbot] + btn_list,
    )

    sandbox_env_choice.change(
        fn=update_sandbox_config,
        inputs=[
            gr.State(value=True),  # Always enabled
            sandbox_env_choice,
            sandbox_state
        ],
        outputs=[sandbox_state]
    ).then(
        # update system prompt when env choice changes
        fn=lambda sandbox_state: gr.update(value=sandbox_state['sandbox_instruction']),
        inputs=[sandbox_state],
        outputs=[system_prompt_textbox]
    )

    # update system prompt when textbox changes
    system_prompt_textbox.change(
        # update sandbox state
        fn=lambda system_prompt_textbox, sandbox_state: update_sandbox_state_system_prompt(sandbox_state, system_prompt_textbox),
        inputs=[system_prompt_textbox, sandbox_state],
        outputs=[sandbox_state]
    )

    # trigger sandbox run when click the code message
    sandbox_components = sandboxes_components[0]
    # trigger sandbox run
    chatbot.select(
        fn=on_click_code_message_run,
        inputs=[state, sandbox_state, *sandbox_components],
        outputs=[*sandbox_components]
    )

    return [state, model_selector]


def build_demo(models) -> gr.Blocks:
    with gr.Blocks(
        title="SWE Arena: Compare & Test Best AI Chatbots for Code",
        theme=gr.themes.Default(),
        css=block_css,
    ) as demo:
        url_params = gr.JSON(visible=False)

        state, model_selector = build_single_model_ui(models)

        if args.model_list_mode not in ["once", "reload"]:
            raise ValueError(f"Unknown model list mode: {args.model_list_mode}")

        if args.show_terms_of_use:
            load_js = get_window_url_params_with_tos_js
        else:
            load_js = get_window_url_params_js

        demo.load(
            load_demo,
            [url_params],
            [
                state,
                model_selector,
            ],
            js=load_js,
        )

    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--share",
        action="store_true",
        help="Whether to generate a public, shareable link",
    )
    parser.add_argument(
        "--controller-url",
        type=str,
        default="http://localhost:21001",
        help="The address of the controller",
    )
    parser.add_argument(
        "--concurrency-count",
        type=int,
        default=10,
        help="The concurrency count of the gradio queue",
    )
    parser.add_argument(
        "--model-list-mode",
        type=str,
        default="once",
        choices=["once", "reload"],
        help="Whether to load the model list once or reload the model list every time",
    )
    parser.add_argument(
        "--moderate",
        action="store_true",
        help="Enable content moderation to block unsafe inputs",
    )
    parser.add_argument(
        "--show-terms-of-use",
        action="store_true",
        help="Shows term of use before loading the demo",
    )
    parser.add_argument(
        "--register-api-endpoint-file",
        type=str,
        help="Register API-based model endpoints from a JSON file",
    )
    parser.add_argument(
        "--gradio-auth-path",
        type=str,
        help='Set the gradio authentication file path. The file should contain one or more user:password pairs in this format: "u1:p1,u2:p2,u3:p3"',
    )
    parser.add_argument(
        "--gradio-root-path",
        type=str,
        help="Sets the gradio root path, eg /abc/def. Useful when running behind a reverse-proxy or at a custom URL path prefix",
    )
    parser.add_argument(
        "--use-remote-storage",
        action="store_true",
        default=False,
        help="Uploads image files to google cloud storage if set to true",
    )
    args = parser.parse_args()
    logger.info(f"args: {args}")

    # Set global variables
    set_global_vars(args.controller_url, args.moderate, args.use_remote_storage)
    models, all_models = get_model_list(
        args.controller_url, args.register_api_endpoint_file, vision_arena=False
    )

    # Set authorization credentials
    auth = None
    if args.gradio_auth_path is not None:
        auth = parse_gradio_auth_creds(args.gradio_auth_path)

    # Launch the demo
    demo = build_demo(models)
    demo.queue(
        default_concurrency_limit=args.concurrency_count,
        status_update_rate=10,
        api_open=False,
    ).launch(
        server_name=args.host,
        server_port=args.port,
        share=args.share,
        max_threads=200,
        auth=auth,
        root_path=args.gradio_root_path,
    )
