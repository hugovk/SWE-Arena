"""
Chatbot Arena (battle) tab.
Users chat with two anonymous models.
"""

import time
import re

import gradio as gr
from gradio_sandboxcomponent import SandboxComponent
import numpy as np

from fastchat.constants import (
    MODERATION_MSG,
    CONVERSATION_LIMIT_MSG,
    SLOW_MODEL_MSG,
    BLIND_MODE_INPUT_CHAR_LEN_LIMIT,
    CONVERSATION_TURN_LIMIT,
    INPUT_CHAR_LEN_LIMIT,
    SURVEY_LINK,
)
from fastchat.model.model_adapter import get_conversation_template
from fastchat.serve.chat_state import LOG_DIR, ModelChatState, save_log_to_local
from fastchat.serve.gradio_block_arena_named import flash_buttons, set_chat_system_messages_multi
from fastchat.serve.gradio_web_server import (
    bot_response,
    no_change_btn,
    enable_btn,
    disable_btn,
    invisible_btn,
    enable_text,
    disable_text,
    acknowledgment_md,
    get_ip,
    get_model_description_md,
    set_chat_system_messages
)
from fastchat.serve.model_sampling import (
    ANON_MODELS,
    BATTLE_STRICT_TARGETS,
    BATTLE_TARGETS,
    OUTAGE_MODELS,
    SAMPLING_BOOST_MODELS,
    SAMPLING_WEIGHTS
)
from fastchat.serve.remote_logger import get_remote_logger

from fastchat.serve.sandbox.sandbox_state import ChatbotSandboxState
from fastchat.serve.sandbox.code_runner import (SUPPORTED_SANDBOX_ENVIRONMENTS, SandboxEnvironment, 
                                            DEFAULT_SANDBOX_INSTRUCTIONS, SandboxGradioSandboxComponents, 
                                            create_chatbot_sandbox_state, on_click_code_message_run, 
                                            on_edit_code, reset_sandbox_state, set_sandbox_state_ids, update_sandbox_config_multi, update_sandbox_state_system_prompt,update_visibility, 
                                            on_edit_dependency)
from fastchat.serve.sandbox.sandbox_telemetry import log_sandbox_telemetry_gradio_fn, save_conv_log_to_azure_storage
from fastchat.utils import (
    build_logger,
    moderation_filter,
)
from fastchat.serve.sandbox.code_analyzer import SandboxEnvironment

logger = build_logger("gradio_web_server_multi", "gradio_web_server_multi.log")

num_sides = 2
enable_moderation = False
anony_names = ["", ""]
models = []


def set_global_vars_anony(enable_moderation_):
    global enable_moderation
    enable_moderation = enable_moderation_


def load_demo_side_by_side_anony(models_, url_params):
    global models
    models = models_

    states = [None] * num_sides
    selector_updates = [
        gr.Markdown(visible=True),
        gr.Markdown(visible=True),
    ]

    return states + selector_updates


def vote_last_response(states: list[ModelChatState], vote_type, model_selectors, request: gr.Request):
    if states[0] is None or states[1] is None:
        yield (None, None) + (disable_text,) + (disable_btn,) * 7
        return

    for state in states:
        local_filepath = state.get_conv_log_filepath(LOG_DIR)
        log_data = state.generate_vote_record(
            vote_type=vote_type,
            ip=get_ip(request),
        )
        save_log_to_local(log_data, local_filepath)
        get_remote_logger().log(log_data)
        # save_conv_log_to_azure_storage(local_filepath.lstrip(LOCAL_LOG_DIR), log_data)

    gr.Info(
        "🎉 Thanks for voting! Your vote shapes the leaderboard, please vote RESPONSIBLY."
    )
    if ":" not in model_selectors[0]:
        for i in range(5):
            names = (
                "### Model A: " + states[0].model_name,
                "### Model B: " + states[1].model_name,
            )
            sandbox_titles = (
                f"### Model A Sandbox: {states[0].model_name}",
                f"### Model B Sandbox: {states[1].model_name}",
            )
            yield names + sandbox_titles + (disable_text,) + (disable_btn,) * 10
            time.sleep(0.1)
    else:
        names = (
            "### Model A: " + states[0].model_name,
            "### Model B: " + states[1].model_name,
        )
        sandbox_titles = (
            f"### Model A Sandbox: {states[0].model_name}",
            f"### Model B Sandbox: {states[1].model_name}",
        )
        yield names + sandbox_titles + (disable_text,) + (disable_btn,) * 10


def leftvote_last_response(
    state0, state1, model_selector0, model_selector1, request: gr.Request
):
    logger.info(f"leftvote (anony). ip: {get_ip(request)}")
    for x in vote_last_response(
        [state0, state1], "leftvote", [model_selector0, model_selector1], request
    ):
        yield x


def rightvote_last_response(
    state0, state1, model_selector0, model_selector1, request: gr.Request
):
    logger.info(f"rightvote (anony). ip: {get_ip(request)}")
    for x in vote_last_response(
        [state0, state1], "rightvote", [model_selector0, model_selector1], request
    ):
        yield x


def tievote_last_response(
    state0, state1, model_selector0, model_selector1, request: gr.Request
):
    logger.info(f"tievote (anony). ip: {get_ip(request)}")
    for x in vote_last_response(
        [state0, state1], "tievote", [model_selector0, model_selector1], request
    ):
        yield x


def bothbad_vote_last_response(
    state0, state1, model_selector0, model_selector1, request: gr.Request
):
    logger.info(f"bothbad_vote (anony). ip: {get_ip(request)}")
    for x in vote_last_response(
        [state0, state1], "bothbad_vote", [model_selector0, model_selector1], request
    ):
        yield x

def regenerate_single(state, request: gr.Request):
    '''
    Regenerate message for one side.
    '''
    logger.info(f"regenerate (anony). ip: {get_ip(request)}")
    if state is None:
        # if not init yet
        return (None, None, "") + (no_change_btn,) * 8
    if not state.regen_support:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "", None) + (no_change_btn,) * 8
    else:
        # if not support regen
        state.conv.update_last_message(None)
        return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 8

def regenerate_multi(state0, state1, request: gr.Request):
    '''
    Regenerate message for both sides.
    '''
    logger.info(f"regenerate_multi (anony). ip: {get_ip(request)}")
    states = [state0, state1]

    if state0 is None and state1 is not None:
        # if state0 is not init yet
        if not state1.regen_support:
            state1.skip_next = True
            return states + [None, state1.to_gradio_chatbot()] + [""] + [no_change_btn] * 8
        state1.conv.update_last_message(None)
        return states + [None, state1.to_gradio_chatbot()] + [""] + [disable_btn] * 8

    if state1 is None and state0 is not None:
        # if state1 is not init yet
        if not state0.regen_support:
            state0.skip_next = True
            return states + [state0.to_gradio_chatbot(), None] + [""] + [no_change_btn] * 8
        state0.conv.update_last_message(None)
        return states + [state0.to_gradio_chatbot(), None] + [""] + [disable_btn] * 8

    if state0.regen_support and state1.regen_support:
        for i in range(num_sides):
            states[i].conv.update_last_message(None)
        return (
            states + [x.to_gradio_chatbot() for x in states] + [""] + [disable_btn] * 8
        )
    states[0].skip_next = True
    states[1].skip_next = True
    return states + [x.to_gradio_chatbot() for x in states] + [""] + [no_change_btn] * 8


def clear_history(sandbox_state0, sandbox_state1, request: gr.Request):
    '''
    Clear chat history for both sides.
    '''
    logger.info(f"clear_history (anony). ip: {get_ip(request)}")
    # reset sandbox state
    sandbox_states = [
        reset_sandbox_state(sandbox_state) for sandbox_state in [sandbox_state0, sandbox_state1]
    ]
    return (
        sandbox_states
        + [None] * num_sides  # states
        + [None] * num_sides  # chatbots
        + anony_names  # model_selectors
        + [enable_text]  # textbox
        + [invisible_btn] * 4  # vote buttons
        + [disable_btn] * 1  # regenerate
        + [invisible_btn] * 2  # regenerate left/right
        + [disable_btn] * 1  # clear
        + [""]  # slow_warning
        + [enable_btn] * 3  # send_btn, send_btn_left, send_btn_right
        + [gr.update(value="### Model A Sandbox"), gr.update(value="### Model B Sandbox")]  # Reset sandbox titles
    )

def clear_sandbox_components(*components):
    updates = []
    for idx, component in enumerate(components):
        if idx in [3, 7]:
            updates.append(gr.update(value=[['', '', '']], visible=False))
        else:
            updates.append(gr.update(value="", visible=False))
    return updates

def share_click(state0, state1, model_selector0, model_selector1, request: gr.Request):
    logger.info(f"share (anony). ip: {get_ip(request)}")
    if state0 is not None and state1 is not None:
        vote_last_response(
            [state0, state1], "share", [model_selector0, model_selector1], request
        )


def get_sample_weight(model, outage_models, sampling_weights, sampling_boost_models=[]):
    if model in outage_models:
        return 0
    weight = sampling_weights.get(model, 0)
    if model in sampling_boost_models:
        weight *= 5
    return weight


def is_model_match_pattern(model, patterns):
    flag = False
    for pattern in patterns:
        pattern = pattern.replace("*", ".*")
        if re.match(pattern, model) is not None:
            flag = True
            break
    return flag


def get_battle_pair(
    models, battle_targets, outage_models, sampling_weights, sampling_boost_models
):
    if len(models) == 1:
        return models[0], models[0]

    model_weights = []
    for model in models:
        weight = get_sample_weight(
            model, outage_models, sampling_weights, sampling_boost_models
        )
        model_weights.append(weight)
    total_weight = np.sum(model_weights)

    model_weights = model_weights / total_weight
    # print(models)
    # print(model_weights)
    chosen_idx = np.random.choice(len(models), p=model_weights)
    chosen_model = models[chosen_idx]
    # for p, w in zip(models, model_weights):
    #     print(p, w)

    rival_models = []
    rival_weights = []
    for model in models:
        if model == chosen_model:
            continue
        if model in ANON_MODELS and chosen_model in ANON_MODELS:
            continue
        if chosen_model in BATTLE_STRICT_TARGETS:
            if not is_model_match_pattern(model, BATTLE_STRICT_TARGETS[chosen_model]):
                continue
        if model in BATTLE_STRICT_TARGETS:
            if not is_model_match_pattern(chosen_model, BATTLE_STRICT_TARGETS[model]):
                continue
        weight = get_sample_weight(model, outage_models, sampling_weights)
        if (
            weight != 0
            and chosen_model in battle_targets
            and model in battle_targets[chosen_model]
        ):
            # boost to 20% chance
            weight = 0.5 * total_weight / len(battle_targets[chosen_model])
        rival_models.append(model)
        rival_weights.append(weight)
    # for p, w in zip(rival_models, rival_weights):
    #     print(p, w)
    rival_weights = rival_weights / np.sum(rival_weights)
    rival_idx = np.random.choice(len(rival_models), p=rival_weights)
    rival_model = rival_models[rival_idx]

    swap = np.random.randint(2)
    if swap == 0:
        return chosen_model, rival_model
    else:
        return rival_model, chosen_model


def add_text_multi(
    state0: ModelChatState, state1: ModelChatState,
    model_selector0: str, model_selector1: str,
    sandbox_state0: ChatbotSandboxState, sandbox_state1: ChatbotSandboxState,
    text: str,
    request: gr.Request
):
    '''
    Add text for both sides.
    '''
    ip = get_ip(request)
    logger.info(f"add_text (anony). ip: {ip}. len: {len(text)}")
    states = [state0, state1]
    sandbox_states = [sandbox_state0, sandbox_state1]
    model_selectors = [model_selector0, model_selector1]

    # increase sandbox state
    sandbox_state0['enabled_round'] += 1
    sandbox_state1['enabled_round'] += 1

    # Init states if necessary
    if states[0] is None:
        assert states[1] is None

        model_left, model_right = get_battle_pair(
            models,
            BATTLE_TARGETS,
            OUTAGE_MODELS,
            SAMPLING_WEIGHTS,
            SAMPLING_BOOST_MODELS,
        )
        states = ModelChatState.create_battle_chat_states(
            model_left, model_right, chat_mode="battle_anony",
            is_vision=False
        )
        states = list(states)
        for idx in range(2):
            set_sandbox_state_ids(
                sandbox_state=sandbox_states[idx],
                conv_id=states[idx].conv_id,
                chat_session_id=states[idx].chat_session_id
            )
        logger.info(f"model: {states[0].model_name}, {states[1].model_name}")

    if len(text) <= 0:
        # skip if no text
        for i in range(num_sides):
            states[i].skip_next = True
        return (
            states
            + [x.to_gradio_chatbot() for x in states]
            + sandbox_states
            + [""]  # textbox
            + [no_change_btn] * sandbox_state0['btn_list_length']  # 8 buttons
        )

    model_list = [states[i].model_name for i in range(num_sides)]
    # turn on moderation in battle mode
    all_conv_text_left = states[0].conv.get_prompt()
    all_conv_text_right = states[0].conv.get_prompt()
    all_conv_text = (
        all_conv_text_left[-1000:] + all_conv_text_right[-1000:] + "\nuser: " + text
    )
    flagged = moderation_filter(all_conv_text, model_list, do_moderation=True)
    if flagged:
        logger.info(f"violate moderation (anony). ip: {ip}. text: {text}")
        # overwrite the original text
        text = MODERATION_MSG

    conv = states[0].conv
    if (len(conv.messages) - conv.offset) // 2 >= CONVERSATION_TURN_LIMIT:
        logger.info(f"conversation turn limit. ip: {get_ip(request)}. text: {text}")
        for i in range(num_sides):
            states[i].skip_next = True
        return (
            states[0], states[1],  # 2 states
            states[0].to_gradio_chatbot(), states[1].to_gradio_chatbot(),  # 2 chatbots
            sandbox_state0, sandbox_state1,  # 2 sandbox states
            CONVERSATION_LIMIT_MSG,  # textbox
            *([no_change_btn] * sandbox_state0['btn_list_length'])  # 8 buttons
        )

    text = text[:BLIND_MODE_INPUT_CHAR_LEN_LIMIT]  # Hard cut-off
    for i in range(num_sides):
        states[i].conv.append_message(states[i].conv.roles[0], text)
        states[i].conv.append_message(states[i].conv.roles[1], None)
        states[i].skip_next = False

    return (
        states[0], states[1],  # 2 states
        states[0].to_gradio_chatbot(), states[1].to_gradio_chatbot(),  # 2 chatbots
        sandbox_state0, sandbox_state1,  # 2 sandbox states
        "",  # textbox
        *([disable_btn] * sandbox_state0['btn_list_length'])  # 8 buttons
    )

def add_text_single(
    state: ModelChatState,
    model_selector: str,
    sandbox_state: ChatbotSandboxState,
    text: str,
    request: gr.Request
):
    '''
    Add text for one side.
    '''
    ip = get_ip(request)
    logger.info(f"add_text. ip: {ip}. len: {len(text)}.")

    # TODO: We should skip this as we should not allow send to one side initially
    if state is None:
        state = ModelChatState(model_selector)

    if state.model_name == "":
        model_left, model_right = get_battle_pair(
            models,
            BATTLE_TARGETS,
            OUTAGE_MODELS,
            SAMPLING_WEIGHTS,
            SAMPLING_BOOST_MODELS,
        )
        state = ModelChatState(model_left)
        logger.info(f"model: {state.model_name}")

    # skip if text is empty
    if len(text) <= 0:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "") + (no_change_btn,) * sandbox_state["btn_list_length"]

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
    if sandbox_state['enable_sandbox']:
         sandbox_state['enabled_round'] += 1 
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * sandbox_state["btn_list_length"]

def bot_response_multi(
    state0,
    state1,
    temperature,
    top_p,
    max_new_tokens,
    sandbox_state0,
    sandbox_state1,
    request: gr.Request,
):
    logger.info(f"bot_response_multi (anony). ip: {get_ip(request)}")

    if state0 is not None and state1 is not None:
        if state0.skip_next or state1.skip_next:
            # This generate call is skipped due to invalid inputs
            yield (
                state0,
                state1,
                state0.to_gradio_chatbot() ,
                state1.to_gradio_chatbot() ,
            ) + (no_change_btn,) * sandbox_state0['btn_list_length']
            return

    states = [state0, state1]
    gen = []
    for i in range(num_sides):
        gen.append(
            bot_response(
                states[i],
                temperature,
                top_p,
                max_new_tokens,
                sandbox_state0,
                request
            )
        )

    model_tpy = []
    for i in range(num_sides):
        token_per_yield = 1
        if states[i] is not None and states[i].model_name in [
            "gemini-pro",
            "gemma-1.1-2b-it",
            "gemma-1.1-7b-it",
            "phi-3-mini-4k-instruct",
            "phi-3-mini-128k-instruct",
            "snowflake-arctic-instruct",
        ]:
            token_per_yield = 30
        elif states[i] is not None and  states[i].model_name in [
            "qwen-max-0428",
            "qwen-vl-max-0809",
            "qwen1.5-110b-chat",
            "llava-v1.6-34b",
        ]:
            token_per_yield = 7
        elif states[i] is not None and states[i].model_name in [
            "qwen2.5-72b-instruct",
            "qwen2-72b-instruct",
            "qwen-plus-0828",
            "qwen-max-0919",
            "llama-3.1-405b-instruct-bf16",
        ]:
            token_per_yield = 4
        model_tpy.append(token_per_yield)

    chatbots = [None] * num_sides
    iters = 0
    while True:
        stop = True
        iters += 1
        for i in range(num_sides):
            try:
                # yield fewer times if chunk size is larger
                if model_tpy[i] == 1 or (iters % model_tpy[i] == 1 or iters < 3):
                    ret = next(gen[i])
                    states[i], chatbots[i] = ret[0], ret[1]
                stop = False
            except StopIteration:
                pass
        yield states + chatbots + [disable_btn] * sandbox_state0['btn_list_length']
        if stop:
            break


def build_side_by_side_ui_anony(models):
    notice_markdown = f"""
## How It Works
- **Blind Test**: Chat with two anonymous AI chatbots and give them a prompt or task (e.g., build a web app, create a visualization, design an interface).
- **Run & Interact**: The AI chatbots generate programs that run in a secure sandbox environment. Test the functionality, explore the features, and evaluate the quality of the outputs.
- **Vote for the Best**: After interacting with both programs, vote for the one that best meets your requirements or provides the superior experience.
- **Play Fair**: If AI identity reveals, your vote won't count.
"""

    states = [gr.State() for _ in range(num_sides)]
    model_selectors = [None] * num_sides
    chatbots: list[gr.Chatbot | None] = [None] * num_sides
    sandbox_titles = [None] * num_sides

    with gr.Blocks():
        with gr.Group(elem_id="share-region-anony"):
            with gr.Row(
                elem_classes=["chatbot-section"],
                max_height='65vh'
            ):
                for i in range(num_sides):
                    label = "Model A" if i == 0 else "Model B"
                    with gr.Column():
                        chatbots[i] = gr.Chatbot(
                            label=label,
                            elem_id="chatbot",
                            height=550,
                            show_copy_button=True,
                            sanitize_html=False,
                            latex_delimiters=[
                                {"left": "$", "right": "$", "display": False},
                                {"left": "$$", "right": "$$", "display": True},
                                {"left": r"\(", "right": r"\)", "display": False},
                                {"left": r"\[", "right": r"\]", "display": True},
                            ],
                        )

            with gr.Row():
                for i in range(num_sides):
                    with gr.Column():
                        model_selectors[i] = gr.Markdown(
                            anony_names[i], elem_id="model_selector_md"
                        )
            with gr.Row():
                slow_warning = gr.Markdown("")

    # sandbox states and components
    sandbox_states: list[gr.State] = [] # state for each chatbot
    sandboxes_components: list[SandboxGradioSandboxComponents] = [] # components for each chatbot
    sandbox_hidden_components = []

    # chatbot sandbox
    with gr.Group():
        # chatbot sandbox config
        with gr.Row():
            sandbox_env_choice = gr.Dropdown(choices=SUPPORTED_SANDBOX_ENVIRONMENTS, label="Programming Expert (Click to select!)", interactive=True, visible=True)
        
            with gr.Accordion("System Prompt (Click to edit!)", open=False) as system_prompt_accordion:
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
                    sandbox_hidden_components.append(sandbox_group)
                    with gr.Row(visible=True) as sandbox_row:
                        sandbox_hidden_components.append(sandbox_row)
                        for chatbotIdx in range(num_sides):
                            with gr.Column(scale=1, visible=True) as column:
                                sandbox_state = gr.State(create_chatbot_sandbox_state(btn_list_length=8))
                                # Add containers for the sandbox output
                                sandbox_titles[chatbotIdx] = gr.Markdown(
                                    value=f"### Model {chr(ord('A') + chatbotIdx)} Sandbox",
                                    visible=True
                                )
                                sandbox_title = sandbox_titles[chatbotIdx]

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
                                            variant='primary',
                                            size='sm'
                                        )
                                    dependency_submit_btn.click(
                                        fn=on_edit_dependency,
                                        inputs=[
                                            states[chatbotIdx],
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
                                # run code when click apply changes
                                sandbox_code_submit_btn.click(
                                    fn=on_edit_code,
                                    inputs=[
                                        states[chatbotIdx],
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

                                sandbox_states.append(sandbox_state)
                                sandboxes_components.append(
                                    (
                                        sandbox_output,
                                        sandbox_ui,
                                        sandbox_code,
                                        sandbox_dependency,
                                    )
                                )
                                sandbox_hidden_components.extend(
                                    [
                                        column,
                                        sandbox_title,
                                        sandbox_output_tab,
                                        sandbox_code_tab,
                                        sandbox_dependency_tab,
                                    ]
                                )

    with gr.Row(elem_id="user-input-region"):
        textbox = gr.Textbox(
            show_label=False,
            placeholder="👉 Enter your prompt and press ENTER",
            elem_id="input_box",
            label = "Type your query",
        )
    
    with gr.Row():
        send_btn = gr.Button(value="⬆️  Send", variant="primary")
        send_btn_left = gr.Button(value="⬅️  Send to Left", variant="primary")
        send_btn_right = gr.Button(value="➡️  Send to Right", variant="primary")
        send_btns_one_side = [send_btn_left, send_btn_right]

    with gr.Row():
        regenerate_btn = gr.Button(value="🔄  Regenerate", interactive=False)
        left_regenerate_btn = gr.Button(value="🔄  Regenerate Left", interactive=False, visible=False)
        right_regenerate_btn = gr.Button(value="🔄  Regenerate Right", interactive=False, visible=False)
        regenerate_one_side_btns = [left_regenerate_btn, right_regenerate_btn]

    with gr.Row():
        leftvote_btn = gr.Button(
            value="👈  A is better", visible=False, interactive=False
        )
        rightvote_btn = gr.Button(
            value="👉  B is better", visible=False, interactive=False
        )
        tie_btn = gr.Button(value="🤝  Tie", visible=False, interactive=False)
        bothbad_btn = gr.Button(
            value="👎  Both are bad", visible=False, interactive=False
        )

    with gr.Row():
        clear_btn = gr.Button(value="🎲 New Round", interactive=False)
        share_btn = gr.Button(value="📷  Share")

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
            inputs = [textbox, sandbox_env_choice]
        )

    with gr.Accordion("Parameters", open=False, visible=False) as parameter_row:
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

    with gr.Group():
        gr.Markdown(notice_markdown, elem_id="notice_markdown")
        gr.Markdown("## Supported Models")
        with gr.Accordion(
            f"🔍 Expand to see the descriptions of {len(models)} models", open=False
        ):
            model_description_md = get_model_description_md(models)
            gr.Markdown(model_description_md, elem_id="model_description_markdown")
        gr.Markdown(acknowledgment_md, elem_id="ack_markdown")

    # Register listeners
    btn_list = [
        # send buttons
        send_btn, send_btn_left, send_btn_right,
        # regenerate buttons
        regenerate_btn, left_regenerate_btn, right_regenerate_btn,
        # vote buttons
        leftvote_btn, rightvote_btn, tie_btn, bothbad_btn,
        # clear button
        clear_btn,
    ]
    leftvote_btn.click(
        leftvote_last_response,
        states + model_selectors,
        model_selectors + sandbox_titles + [
            textbox,
            # vote buttons
            leftvote_btn, rightvote_btn, tie_btn, bothbad_btn,
            # send buttons
            send_btn, send_btn_left, send_btn_right,
            # regenerate buttons
            regenerate_btn, left_regenerate_btn, right_regenerate_btn,
        ]
    )
    rightvote_btn.click(
        rightvote_last_response,
        states + model_selectors,
        model_selectors + sandbox_titles + [
            textbox,
            # vote buttons
            leftvote_btn, rightvote_btn, tie_btn, bothbad_btn,
            # send buttons
            send_btn, send_btn_left, send_btn_right,
            # regenerate buttons
            regenerate_btn, left_regenerate_btn, right_regenerate_btn,
        ]
    )
    tie_btn.click(
        tievote_last_response,
        states + model_selectors,
        model_selectors + sandbox_titles + [
            textbox,
            # vote buttons
            leftvote_btn, rightvote_btn, tie_btn, bothbad_btn,
            # send buttons
            send_btn, send_btn_left, send_btn_right,
            # regenerate buttons
            regenerate_btn, left_regenerate_btn, right_regenerate_btn,
        ]
    )
    bothbad_btn.click(
        bothbad_vote_last_response,
        states + model_selectors,
        model_selectors + sandbox_titles + [
            textbox,
            # vote buttons
            leftvote_btn, rightvote_btn, tie_btn, bothbad_btn,
            # send buttons
            send_btn, send_btn_left, send_btn_right,
            # regenerate buttons
            regenerate_btn, left_regenerate_btn, right_regenerate_btn,
        ]
    )
    regenerate_btn.click(
        regenerate_multi,
        states,
        states + chatbots + [textbox] + btn_list
    ).then(
        bot_response_multi,
        states + [temperature, top_p, max_output_tokens] + sandbox_states,
        states + chatbots + btn_list,
    ).then(
        flash_buttons, [], btn_list
    )
    clear_btn.click(
        clear_history,
        sandbox_states,
        sandbox_states
        + states
        + chatbots
        + model_selectors
        + [textbox]
        + btn_list
        + [slow_warning]
        + [send_btn, send_btn_left, send_btn_right]
        + sandbox_titles,  # Add sandbox titles to outputs
    ).then(
        clear_sandbox_components,
        inputs=[component for components in sandboxes_components for component in components],
        outputs=[component for components in sandboxes_components for component in components]
    ).then(
        lambda: (gr.update(interactive=True, value=SandboxEnvironment.AUTO), gr.update(interactive=True, value=DEFAULT_SANDBOX_INSTRUCTIONS[SandboxEnvironment.AUTO])),
        outputs=[sandbox_env_choice, system_prompt_textbox]
    ).then(
        lambda: gr.update(visible=True),
        outputs=examples_row
    )

    share_js = """
function (a, b, c, d) {
    const captureElement = document.querySelector('#share-region-anony');
    html2canvas(captureElement)
        .then(canvas => {
            canvas.style.display = 'none'
            document.body.appendChild(canvas)
            return canvas
        })
        .then(canvas => {
            const image = canvas.toDataURL('image/png')
            const a = document.createElement('a')
            a.setAttribute('download', 'chatbot-arena.png')
            a.setAttribute('href', image)
            a.click()
            canvas.remove()
        });
    return [a, b, c, d];
}
"""
    share_btn.click(share_click, states + model_selectors, [], js=share_js)

    textbox.submit(
        add_text_multi,
        states + model_selectors + sandbox_states + [textbox],
        states + chatbots + sandbox_states + [textbox] + btn_list,
    ).then(
        set_chat_system_messages_multi,
        states + sandbox_states + model_selectors,
        states + chatbots
    ).then(
        # hide the examples row
        lambda: gr.update(visible=False),
        outputs=examples_row
    ).then(
        bot_response_multi,
        states + [temperature, top_p, max_output_tokens] + sandbox_states,
        states + chatbots + btn_list,
    ).then(
        flash_buttons,
        [],
        btn_list,
    ).then(
        fn=lambda sandbox_state: [
            gr.update(interactive=sandbox_state['enabled_round'] == 0),
            gr.update(interactive=sandbox_state['enabled_round'] == 0)
        ],
        inputs=[sandbox_states[0]],
        outputs=[system_prompt_textbox, sandbox_env_choice]
    )

    send_btn.click(
        add_text_multi,
        states + model_selectors + sandbox_states + [textbox],
        states + chatbots + sandbox_states + [textbox] + btn_list,
    ).then(
        set_chat_system_messages_multi,
        states + sandbox_states + model_selectors,
        states + chatbots
    ).then(
        lambda: gr.update(visible=False),
        inputs=None,
        outputs=examples_row
    ).then(
        bot_response_multi,
        states + [temperature, top_p, max_output_tokens] + sandbox_states,
        states + chatbots + btn_list,
    ).then(
        flash_buttons, [], btn_list
    ).then(
        fn=lambda sandbox_state: [
            gr.update(interactive=sandbox_state['enabled_round'] == 0),
            gr.update(interactive=sandbox_state['enabled_round'] == 0)
        ],
        inputs=[sandbox_states[0]],
        outputs=[system_prompt_textbox, sandbox_env_choice]
    )

    # update state when env choice changes
    sandbox_env_choice.change(
        # update sandbox state
        fn=update_sandbox_config_multi,
        inputs=[
            gr.State(value=True),
            sandbox_env_choice,
            *sandbox_states
        ],
        outputs=[*sandbox_states]
    ).then(
        # update system prompt when env choice changes
        fn=lambda sandbox_state: gr.update(value=sandbox_state['sandbox_instruction']),
        inputs=[sandbox_states[0]],
        outputs=[system_prompt_textbox]
    )

    # update system prompt when textbox changes
    system_prompt_textbox.change(
        # update sandbox state
        fn=lambda system_prompt_textbox, sandbox_state0, sandbox_state1: [
            update_sandbox_state_system_prompt(state, system_prompt_textbox) for state in (sandbox_state0, sandbox_state1)
        ],
        inputs=[system_prompt_textbox, sandbox_states[0], sandbox_states[1]],
        outputs=[sandbox_states[0], sandbox_states[1]]
    )

    for chatbotIdx in range(num_sides):
        chatbot = chatbots[chatbotIdx]
        state = states[chatbotIdx]
        sandbox_state = sandbox_states[chatbotIdx]
        sandbox_components = sandboxes_components[chatbotIdx]
        model_selector = model_selectors[chatbotIdx]

        send_btns_one_side[chatbotIdx].click(
            add_text_single,
            [state, model_selector, sandbox_state, textbox],
            [state, chatbot, textbox] + btn_list,
        ).then(
            set_chat_system_messages,
            [state, sandbox_state, model_selector],
            [state, chatbot]
        ).then(
            lambda: gr.update(visible=False),
            inputs=None,
            outputs=examples_row
        ).then(
            bot_response,
            [state, temperature, top_p, max_output_tokens, sandbox_state],
            [state, chatbot] + btn_list,
        ).then(
            flash_buttons, [], btn_list
        ).then(
            fn=lambda sandbox_state: [
                gr.update(interactive=sandbox_state['enabled_round'] == 0),
                gr.update(interactive=sandbox_state['enabled_round'] == 0)
            ],
            inputs=[sandbox_state],
            outputs=[system_prompt_textbox, sandbox_env_choice]
        )

        regenerate_one_side_btns[chatbotIdx].click(regenerate_single, state, [state, chatbot, textbox] + btn_list
        ).then(
            bot_response,
            [state, temperature, top_p, max_output_tokens, sandbox_state],
            [state, chatbot] + btn_list,
       )

        # trigger sandbox run when click code message
        chatbot.select(
            fn=on_click_code_message_run,
            inputs=[state, sandbox_state, *sandbox_components],
            outputs=[*sandbox_components],
        )

    return states + model_selectors
