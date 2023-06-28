import os
import os.path as osp
import time
import uuid
from argparse import ArgumentParser
from functools import partial

import gradio as gr
import numpy as np
import torch
from PIL import Image

import dnnlib
from gradio_utils import (ImageMask, draw_mask_on_image, draw_points_on_image,
                          get_latest_points_pair, get_valid_mask,
                          on_change_single_global_state)
from viz.renderer import Renderer, add_watermark_np

try:
    from openxlab.model import download
    is_openxlab = True
except Exception:
    is_openxlab = False

torch.backends.cudnn.enabled = False

parser = ArgumentParser()
parser.add_argument('--share', action='store_true')

parser.add_argument('--max-size', type=int, default=50)
parser.add_argument('--concurrency-count', type=int, default=3)

parser.add_argument('--host', type=str)
parser.add_argument('--port', type=int)

parser.add_argument('--max-step', type=int, default=500)
parser.add_argument('--cache-dir', type=str, default='./checkpoints')

parser.add_argument('--disable-queue', action='store_true')
parser.add_argument('--log-level', choices=['debug', 'info'])

args = parser.parse_args()

MAX_STEP = args.max_step
disable_queue = args.disable_queue
LOG_LEVEL = args.log_level
# cache_dir = args.cache_dir


def get_curr_time():
    # return int(round(time.time()) * 1000)
    return time.time() * 1000


if is_openxlab:
    cache_dir = '/home/xlab-app-center/.cache/model'
    # os.makedirs(cache_dir, exist_ok=True)
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Human')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Dogs')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Elephants')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Horse')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Lions')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Cats')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Car-f')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-Cat-f')
    # download(model_repo='mmagic/DragGAN',
    #          model_name='DragGAN-FFHQ')
else:
    cache_dir = args.cache_dir

device = 'cuda'


def reverse_point_pairs(points):
    new_points = []
    for p in points:
        new_points.append([p[1], p[0]])
    return new_points


def clear_state(global_state, target=None):
    """Clear target history state from global_state
    If target is not defined, points and mask will be both removed.
    1. set global_state['points'] as empty dict
    2. set global_state['mask'] as full-one mask.
    """
    if target is None:
        target = ['point', 'mask']
    if not isinstance(target, list):
        target = [target]
    if 'point' in target:
        global_state['points'] = dict()
        print_log('Clear Points State!')
    if 'mask' in target:
        image_raw = global_state["images"]["image_raw"]
        global_state['mask'] = np.ones((image_raw.size[1], image_raw.size[0]),
                                       dtype=np.uint8)
        print_log('Clear mask State!')

    return global_state


def init_images(global_state):
    """This function is called only ones with Gradio App is started.
    0. pre-process global_state, unpack value from global_state of need
    1. Re-init renderer
    2. run `renderer._render_drag_impl` with `is_drag=False` to generate
       new image
    3. Assign images to global state and re-generate mask
    """

    if isinstance(global_state, gr.State):
        state = global_state.value
    else:
        state = global_state

    state['renderer'].init_network(
        state['generator_params'],  # res
        valid_checkpoints_dict[state['pretrained_weight']],  # pkl
        state['params']['seed'],  # w0_seed,
        None,  # w_load
        state['params']['latent_space'] == 'w+',  # w_plus
        'const',
        state['params']['trunc_psi'],  # trunc_psi,
        state['params']['trunc_cutoff'],  # trunc_cutoff,
        None,  # input_transform
        state['params']['lr']  # lr,
    )

    state['renderer']._render_drag_impl(state['generator_params'],
                                        is_drag=False,
                                        to_pil=True)

    init_image = state['generator_params'].image
    state['images']['image_orig'] = init_image
    state['images']['image_raw'] = init_image
    state['images']['image_show'] = Image.fromarray(
        add_watermark_np(np.array(init_image)))
    state['mask'] = np.ones((init_image.size[1], init_image.size[0]),
                            dtype=np.uint8)
    return global_state


def update_image_draw(image, points, mask, show_mask, global_state=None):

    image_draw = draw_points_on_image(image, points)
    if show_mask and mask is not None and not (mask == 0).all() and not (
            mask == 1).all():
        image_draw = draw_mask_on_image(image_draw, mask)

    image_draw = Image.fromarray(add_watermark_np(np.array(image_draw)))
    if global_state is not None:
        global_state['images']['image_show'] = image_draw
    return image_draw


def preprocess_mask_info(global_state, image):
    """Function to handle mask information.
    1. last_mask is None: Do not need to change mask, return mask
    2. last_mask is not None:
        2.1 global_state is remove_mask:
        2.2 global_state is add_mask:
    """
    if isinstance(image, dict):
        last_mask = get_valid_mask(image['mask'])
    else:
        last_mask = None
    mask = global_state['mask']

    # mask in global state is a placeholder with all 1.
    if (mask == 1).all():
        mask = last_mask

    # last_mask = global_state['last_mask']
    editing_mode = global_state['editing_state']

    if last_mask is None:
        return global_state

    if editing_mode == 'remove_mask':
        updated_mask = np.clip(mask - last_mask, 0, 1)
        print_log(f'Last editing_state is {editing_mode}, do remove.')
    elif editing_mode == 'add_mask':
        updated_mask = np.clip(mask + last_mask, 0, 1)
        print_log(f'Last editing_state is {editing_mode}, do add.')
    else:
        updated_mask = mask
        print_log(f'Last editing_state is {editing_mode}, '
                  'do nothing to mask.')

    global_state['mask'] = updated_mask
    # global_state['last_mask'] = None  # clear buffer
    return global_state


valid_checkpoints_dict = {
    f.split('/')[-1].split('.')[0]: osp.join(cache_dir, f)
    for f in os.listdir(cache_dir)
    if (f.endswith('pkl') and osp.exists(osp.join(cache_dir, f)))
}
print(f'File under cache_dir ({cache_dir}):')
print(os.listdir(cache_dir))
print('Valid checkpoint file:')
print(valid_checkpoints_dict)

init_pkl = 'stylegan2_lions_512_pytorch'

with gr.Blocks() as app:

    def print_log(cont, uid=None):
        prefix_list = []
        if uid is not None:
            prefix_list.append(f'[{uid}]')
        prefix_list.append(f'[{get_curr_time()}]')
        prefix = ''.join(prefix_list)
        print(f'{prefix} {cont}')

    # renderer = Renderer()
    global_state = gr.State({
        "images": {
            # image_orig: the original image, change with seed/model is changed
            # image_raw: image with mask and points, change durning optimization
            # image_show: image showed on screen
        },
        "temporal_params": {
            # stop
        },
        'mask':
        None,  # mask for visualization, 1 for editing and 0 for unchange
        'last_mask': None,  # last edited mask
        'show_mask': True,  # add button
        "generator_params": dnnlib.EasyDict(),
        "params": {
            "seed": 0,
            "motion_lambda": 20,
            "r1_in_pixels": 3,
            "r2_in_pixels": 12,
            "magnitude_direction_in_pixels": 1.0,
            "latent_space": "w+",
            "trunc_psi": 0.7,
            "trunc_cutoff": None,
            "lr": 0.001,
        },
        "device": device,
        "draw_interval": 1,
        "renderer": Renderer(disable_timing=True),
        "points": {},
        "curr_point": None,
        "curr_type_point": "start",
        'editing_state': 'add_points',
        'pretrained_weight': init_pkl
    })

    # init image
    global_state = init_images(global_state)

    # Header~
    with gr.Row():
        gr.HTML("""
            <h1 align="center">The Official Implementation of </h1>
            <h1 align="center"><a href="https://github.com/XingangPan/DragGAN">"Drag Your GAN: Interactive Point-based Manipulation on the Generative Image Manifold"</a></h1>
            <br>
            """)
        # gr.HTML("""
        #     <h1 align="center"><a href="https://github.com/XingangPan/DragGAN">"Drag Your GAN: Interactive Point-based Manipulation on the Generative Image Manifold"</a></h1>
        #     <br>
        #     """)

    # with gr.Row():
    #     gr.Markdown("""
    #         * Official GitHub Repo: [DragGAN](https://github.com/XingangPan/DragGAN)
    #         """)

    with gr.Row():

        with gr.Row():

            # Left --> tools
            with gr.Column(scale=3):

                # Pickle
                with gr.Row():

                    with gr.Column(scale=1, min_width=10):
                        gr.Markdown(value='Pickle', show_label=False)

                    with gr.Column(scale=4, min_width=10):
                        form_pretrained_dropdown = gr.Dropdown(
                            choices=list(valid_checkpoints_dict.keys()),
                            label="Pretrained Model",
                            value=init_pkl,
                        )

                # Latent
                with gr.Row():
                    with gr.Column(scale=1, min_width=10):
                        gr.Markdown(value='Latent', show_label=False)

                    with gr.Column(scale=4, min_width=10):
                        form_seed_number = gr.Number(
                            value=global_state.value['params']['seed'],
                            interactive=True,
                            label="Seed",
                        )
                        form_lr_number = gr.Number(
                            value=global_state.value["params"]["lr"],
                            interactive=True,
                            label="Step Size")

                        with gr.Row():
                            with gr.Column(scale=2, min_width=10):
                                form_reset_image = gr.Button("Reset Image")
                            with gr.Column(scale=3, min_width=10):
                                form_latent_space = gr.Radio(
                                    ['w', 'w+'],
                                    value=global_state.value['params']
                                    ['latent_space'],
                                    interactive=True,
                                    label='Latent space to optimize',
                                    show_label=False,
                                )

                # Drag
                with gr.Row():
                    with gr.Column(scale=1, min_width=10):
                        gr.Markdown(value='Drag', show_label=False)
                    with gr.Column(scale=4, min_width=10):
                        with gr.Row():
                            with gr.Column(scale=1, min_width=10):
                                enable_add_points = gr.Button('Add Points')
                            with gr.Column(scale=1, min_width=10):
                                undo_points = gr.Button('Reset Points')
                        with gr.Row():
                            with gr.Column(scale=1, min_width=10):
                                form_start_btn = gr.Button("Start")
                            with gr.Column(scale=1, min_width=10):
                                form_stop_btn = gr.Button("Stop")

                        form_steps_number = gr.Number(value=0,
                                                      label="Steps",
                                                      interactive=False)

                # Mask
                with gr.Row():
                    with gr.Column(scale=1, min_width=10):
                        gr.Markdown(value='Mask', show_label=False)
                    with gr.Column(scale=4, min_width=10):
                        enable_add_mask = gr.Button('Edit Flexible Area')
                        with gr.Row():
                            with gr.Column(scale=1, min_width=10):
                                form_reset_mask_btn = gr.Button("Reset mask")
                            with gr.Column(scale=1, min_width=10):
                                show_mask = gr.Checkbox(
                                    label='Show Mask',
                                    value=global_state.value['show_mask'],
                                    show_label=False)

                        with gr.Row():
                            form_lambda_number = gr.Number(
                                value=global_state.value["params"]
                                ["motion_lambda"],
                                interactive=True,
                                label="Lambda",
                            )

                form_draw_interval_number = gr.Number(
                    value=global_state.value["draw_interval"],
                    label="Draw Interval (steps)",
                    interactive=True,
                    visible=False)

            # Mid --> Image
            with gr.Column(scale=8):
                form_image = ImageMask(
                    value=global_state.value['images']['image_show'],
                    brush_radius=20).style(
                        width=768,
                        height=768)  # NOTE: hard image size code here.

            # Right --> Instruction
            # with gr.Column(scale=2):
            #     gr.Markdown("""
            #         ## Quick Start

            #         1. Select desired `Pretrained Model` and adjust `Seed` to generate an
            #         initial image.
            #         2. Click on image to add control points.
            #         3. Click `Start` and enjoy it!

            #         ## Advance Usage

            #         1. Change `Step Size` to adjust learning rate in drag optimization.
            #         2. Select `w` or `w+` to change latent space to optimize:
            #         * Optimize on `w` space may cause greater influence to the image.
            #         * Optimize on `w+` space may work slower than `w`, but usually achieve
            #         better results.
            #         * Note that changing the latent space will reset the image, points and
            #         mask (this has the same effect as `Reset Image` button).
            #         3. Click `Edit Flexible Area` to create a mask and constrain the
            #         unmasked region to remain unchanged.
            #         """)

    # Network & latents tab listeners
    def on_change_pretrained_dropdown(pretrained_value, global_state):
        """Function to handle model change.
        1. Set pretrained value to global_state
        2. Re-init images and clear all states
        """

        global_state['pretrained_weight'] = pretrained_value
        init_images(global_state)
        clear_state(global_state)

        return global_state, global_state["images"]['image_show']

    form_pretrained_dropdown.change(
        on_change_pretrained_dropdown,
        inputs=[form_pretrained_dropdown, global_state],
        outputs=[global_state, form_image])

    def on_click_reset_image(global_state):
        """Reset image to the original one and clear all states
        1. Re-init images
        2. Clear all states
        """

        init_images(global_state)
        clear_state(global_state)

        return global_state, global_state['images']['image_show']

    form_reset_image.click(on_click_reset_image,
                           inputs=[global_state],
                           outputs=[global_state, form_image])

    # Update parameters
    def on_change_update_image_seed(seed, global_state):
        """Function to handle generation seed change.
        1. Set seed to global_state
        2. Re-init images and clear all states
        """

        global_state["params"]["seed"] = int(seed)
        init_images(global_state)
        clear_state(global_state)

        return global_state, global_state['images']['image_show']

    form_seed_number.change(on_change_update_image_seed,
                            inputs=[form_seed_number, global_state],
                            outputs=[global_state, form_image])

    def on_click_latent_space(latent_space, global_state):
        """Function to reset latent space to optimize.
        NOTE: this function we reset the image and all controls
        1. Set latent-space to global_state
        2. Re-init images and clear all state
        """

        global_state['params']['latent_space'] = latent_space
        init_images(global_state)
        clear_state(global_state)

        return global_state, global_state['images']['image_show']

    form_latent_space.change(on_click_latent_space,
                             inputs=[form_latent_space, global_state],
                             outputs=[global_state, form_image])

    # ==== Params
    form_lambda_number.change(partial(on_change_single_global_state,
                                      ["params", "motion_lambda"]),
                              inputs=[form_lambda_number, global_state],
                              outputs=[global_state],
                              queue=not disable_queue)

    def on_change_lr(lr, global_state):
        if lr == 0:
            print_log('lr is 0, do nothing.')
            return global_state
        else:
            global_state["params"]["lr"] = lr
            renderer = global_state['renderer']
            renderer.update_lr(lr)
            print_log('New optimizer: ')
            print_log(renderer.w_optim)
        return global_state

    form_lr_number.change(on_change_lr,
                          inputs=[form_lr_number, global_state],
                          outputs=[global_state],
                          queue=not disable_queue)

    def on_click_start(global_state, image):

        uid = str(uuid.uuid4()).split('-')[-1]

        p_in_pixels = []
        t_in_pixels = []
        valid_points = []

        # handle of start drag in mask editing mode
        global_state = preprocess_mask_info(global_state, image)

        # Prepare the points for the inference

        # skip drag if point pair is not finished
        skip_drag = False
        if len(global_state["points"]) == 0:
            skip_drag = True
        else:
            last_point_idx = get_latest_points_pair(global_state['points'])
            if 'target' not in global_state['points'][last_point_idx]:
                skip_drag = True
            elif global_state['points'][last_point_idx]['target'] is None:
                skip_drag = True

        if skip_drag:
            # yield on_click_start_wo_points(global_state, image)
            image_raw = global_state['images']['image_raw']
            update_image_draw(
                image_raw,
                global_state['points'],
                global_state['mask'],
                global_state['show_mask'],
                global_state,
            )

            yield (
                global_state,
                0,
                global_state['images']['image_show'],
                # gr.File.update(visible=False),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                # latent space
                gr.Radio.update(interactive=True),
                gr.Button.update(interactive=True),
                # NOTE: disable stop button
                gr.Button.update(interactive=False),

                # update other comps
                gr.Dropdown.update(interactive=True),
                gr.Number.update(interactive=True),
                gr.Number.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Checkbox.update(interactive=True),
                # gr.Number.update(interactive=True),
                gr.Number.update(interactive=True),
            )
        else:

            # Transform the points into torch tensors
            for key_point, point in global_state["points"].items():
                try:
                    p_start = point.get("start_temp", point["start"])
                    p_end = point["target"]

                    if p_start is None or p_end is None:
                        continue

                except KeyError:
                    continue

                p_in_pixels.append(p_start)
                t_in_pixels.append(p_end)
                valid_points.append(key_point)

            mask = torch.tensor(global_state['mask']).float()
            drag_mask = 1 - mask

            renderer: Renderer = global_state["renderer"]
            global_state['temporal_params']['stop'] = False
            global_state['editing_state'] = 'running'

            # reverse points order
            p_to_opt = reverse_point_pairs(p_in_pixels)
            t_to_opt = reverse_point_pairs(t_in_pixels)
            print_log('Running with:')
            print_log(f'    Source: {p_in_pixels}')
            print_log(f'    Target: {t_in_pixels}')
            step_idx = 0
            while True:
                if global_state["temporal_params"]["stop"]:
                    print_log('Stop Drag by STOP.', uid)
                    break
                if step_idx > MAX_STEP:
                    print_log(f'Reach Max Step ({MAX_STEP}), Stop!', uid)
                    break

                # do drage here!
                start_time = get_curr_time()
                print_log(f'Drag step {step_idx}, start', uid)
                renderer._render_drag_impl(
                    global_state['generator_params'],
                    p_to_opt,  # point
                    t_to_opt,  # target
                    drag_mask,  # mask,
                    global_state['params']['motion_lambda'],  # lambda_mask
                    reg=0,
                    feature_idx=5,  # NOTE: do not support change for now
                    r1=global_state['params']['r1_in_pixels'],  # r1
                    r2=global_state['params']['r2_in_pixels'],  # r2
                    # random_seed     = 0,
                    # noise_mode      = 'const',
                    trunc_psi=global_state['params']['trunc_psi'],
                    # force_fp32      = False,
                    # layer_name      = None,
                    # sel_channels    = 3,
                    # base_channel    = 0,
                    # img_scale_db    = 0,
                    # img_normalize   = False,
                    # untransform     = False,
                    is_drag=True,
                    to_pil=True)
                end_time = get_curr_time()

                print_log(f'Drag step {step_idx}, end, time cost: '
                          f'{end_time-start_time}', uid)

                _should_stop = global_state['generator_params']['stop']
                if _should_stop:
                    print_log('Optimization Finish. Stop Drag.', uid)
                    break

                if step_idx % global_state['draw_interval'] == 0:
                    # print_log('Current Source:')
                    for key_point, p_i, t_i in zip(valid_points, p_to_opt,
                                                   t_to_opt):
                        global_state["points"][key_point]["start_temp"] = [
                            p_i[1],
                            p_i[0],
                        ]
                        global_state["points"][key_point]["target"] = [
                            t_i[1],
                            t_i[0],
                        ]
                        # start_temp = global_state["points"][key_point][
                        #     "start_temp"]
                        # print_log(f'    {start_temp}')

                    image_result = global_state['generator_params']['image']
                    image_draw = update_image_draw(
                        image_result,
                        global_state['points'],
                        global_state['mask'],
                        global_state['show_mask'],
                        global_state,
                    )
                    global_state['images']['image_raw'] = image_result

                yield (
                    global_state,
                    step_idx,
                    global_state['images']['image_show'],
                    # gr.File.update(visible=False),
                    gr.Button.update(interactive=False),
                    gr.Button.update(interactive=False),
                    gr.Button.update(interactive=False),
                    gr.Button.update(interactive=False),
                    gr.Button.update(interactive=False),
                    # latent space
                    gr.Radio.update(interactive=False),
                    gr.Button.update(interactive=False),
                    # enable stop button in loop
                    gr.Button.update(interactive=True),

                    # update other comps
                    gr.Dropdown.update(interactive=False),
                    gr.Number.update(interactive=False),
                    gr.Number.update(interactive=False),
                    gr.Button.update(interactive=False),
                    gr.Button.update(interactive=False),
                    gr.Checkbox.update(interactive=False),
                    # gr.Number.update(interactive=False),
                    gr.Number.update(interactive=False),
                )

                # increate step
                step_idx += 1

            image_result = global_state['generator_params']['image']
            global_state['images']['image_raw'] = image_result
            image_draw = update_image_draw(image_result,
                                           global_state['points'],
                                           global_state['mask'],
                                           global_state['show_mask'],
                                           global_state)

            # fp = NamedTemporaryFile(suffix=".png", delete=False)
            # image_result.save(fp, "PNG")

            global_state['editing_state'] = 'add_points'

            yield (
                global_state,
                0,  # reset step to 0 after stop.
                global_state['images']['image_show'],
                # gr.File.update(visible=True, value=fp.name),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                gr.Button.update(interactive=True),
                # latent space
                gr.Radio.update(interactive=True),
                gr.Button.update(interactive=True),
                # NOTE: disable stop button with loop finish
                gr.Button.update(interactive=False),

                # update other comps
                gr.Dropdown.update(interactive=True),
                gr.Number.update(interactive=True),
                gr.Number.update(interactive=True),
                gr.Checkbox.update(interactive=True),
                gr.Number.update(interactive=True),
            )

    form_start_btn.click(
        on_click_start,
        inputs=[global_state, form_image],
        outputs=[
            global_state,
            form_steps_number,
            form_image,
            # form_download_result_file,
            # >>> buttons
            form_reset_image,
            enable_add_points,
            enable_add_mask,
            undo_points,
            form_reset_mask_btn,
            form_latent_space,
            form_start_btn,
            form_stop_btn,
            # <<< buttonm
            # >>> inputs comps
            form_pretrained_dropdown,
            form_seed_number,
            form_lr_number,
            show_mask,
            form_lambda_number,
        ],
    )

    def on_click_stop(global_state):
        """Function to handle stop button is clicked.
        1. send a stop signal by set global_state["temporal_params"]["stop"] as True
        2. Disable Stop button
        """
        global_state["temporal_params"]["stop"] = True

        return global_state, gr.Button.update(interactive=False)

    form_stop_btn.click(on_click_stop,
                        inputs=[global_state],
                        outputs=[global_state, form_stop_btn],
                        queue=not disable_queue)

    form_draw_interval_number.change(
        partial(
            on_change_single_global_state,
            "draw_interval",
            map_transform=lambda x: int(x),
        ),
        inputs=[form_draw_interval_number, global_state],
        outputs=[global_state],
    )

    def on_click_remove_point(global_state):
        choice = global_state["curr_point"]
        del global_state["points"][choice]

        choices = list(global_state["points"].keys())

        if len(choices) > 0:
            global_state["curr_point"] = choices[0]

        return (
            gr.Dropdown.update(choices=choices, value=choices[0]),
            global_state,
        )

    # Mask
    def on_click_reset_mask(global_state):
        global_state['mask'] = np.ones(
            (
                global_state["images"]["image_raw"].size[1],
                global_state["images"]["image_raw"].size[0],
            ),
            dtype=np.uint8,
        )
        image_draw = update_image_draw(global_state['images']['image_raw'],
                                       global_state['points'],
                                       global_state['mask'],
                                       global_state['show_mask'], global_state)
        return global_state, image_draw

    form_reset_mask_btn.click(on_click_reset_mask,
                              inputs=[global_state],
                              outputs=[global_state, form_image],
                              queue=not disable_queue)

    # Image
    def on_click_enable_draw(global_state, image):
        """Function to start add mask mode.
        1. Preprocess mask info from last state
        2. Change editing state to add_mask
        3. Set curr image with points and mask
        """
        global_state = preprocess_mask_info(global_state, image)
        global_state['editing_state'] = 'add_mask'
        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(image_raw, global_state['points'],
                                       global_state['mask'], True,
                                       global_state)
        return (global_state,
                gr.Image.update(value=image_draw, interactive=True))

    def on_click_remove_draw(global_state, image):
        """Function to start remove mask mode.
        1. Preprocess mask info from last state
        2. Change editing state to remove_mask
        3. Set curr image with points and mask
        """
        global_state = preprocess_mask_info(global_state, image)
        global_state['edinting_state'] = 'remove_mask'
        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(image_raw, global_state['points'],
                                       global_state['mask'], True,
                                       global_state)
        return (global_state,
                gr.Image.update(value=image_draw, interactive=True))

    enable_add_mask.click(on_click_enable_draw,
                          inputs=[global_state, form_image],
                          outputs=[
                              global_state,
                              form_image,
                          ],
                          queue=not disable_queue)

    def on_click_add_point(global_state, image: dict):
        """Function switch from add mask mode to add points mode.
        1. Updaste mask buffer if need
        2. Change global_state['editing_state'] to 'add_points'
        3. Set current image with mask
        """

        global_state = preprocess_mask_info(global_state, image)
        global_state['editing_state'] = 'add_points'
        mask = global_state['mask']
        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(image_raw, global_state['points'], mask,
                                       global_state['show_mask'], global_state)

        return (global_state,
                gr.Image.update(value=image_draw, interactive=False))

    enable_add_points.click(on_click_add_point,
                            inputs=[global_state, form_image],
                            outputs=[global_state, form_image],
                            queue=not disable_queue)

    def on_click_image(global_state, evt: gr.SelectData):
        """This function only support click for point selection
        """
        xy = evt.index
        if global_state['editing_state'] != 'add_points':
            print_log(f'In {global_state["editing_state"]} state. '
                      'Do not add points.')

            return global_state, global_state['images']['image_show']

        points = global_state["points"]

        point_idx = get_latest_points_pair(points)
        if point_idx is None:
            points[0] = {'start': xy, 'target': None}
            print_log(f'Click Image - Start - {xy}')
        elif points[point_idx].get('target', None) is None:
            points[point_idx]['target'] = xy
            print_log(f'Click Image - Target - {xy}')
        else:
            points[point_idx + 1] = {'start': xy, 'target': None}
            print_log(f'Click Image - Start - {xy}')

        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(
            image_raw,
            global_state['points'],
            global_state['mask'],
            global_state['show_mask'],
            global_state,
        )

        return global_state, image_draw

    form_image.select(on_click_image,
                      inputs=[global_state],
                      outputs=[global_state, form_image],
                      queue=not disable_queue)

    def on_click_clear_points(global_state):
        """Function to handle clear all control points
        1. clear global_state['points'] (clear_state)
        2. re-init network
        2. re-draw image
        """
        clear_state(global_state, target='point')

        renderer: Renderer = global_state["renderer"]
        renderer.feat_refs = None

        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(image_raw, {}, global_state['mask'],
                                       global_state['show_mask'], global_state)
        return global_state, image_draw

    undo_points.click(on_click_clear_points,
                      inputs=[global_state],
                      outputs=[global_state, form_image],
                      queue=not disable_queue)

    def on_click_show_mask(global_state, show_mask):
        """Function to control whether show mask on image."""
        global_state['show_mask'] = show_mask

        image_raw = global_state['images']['image_raw']
        image_draw = update_image_draw(
            image_raw,
            global_state['points'],
            global_state['mask'],
            global_state['show_mask'],
            global_state,
        )
        return global_state, image_draw

    show_mask.change(on_click_show_mask,
                     inputs=[global_state, show_mask],
                     outputs=[global_state, form_image],
                     queue=not disable_queue)

gr.close_all()
app.queue(concurrency_count=args.concurrency_count, max_size=args.max_size)
app.launch(share=args.share, server_name=args.host, server_port=args.port)
