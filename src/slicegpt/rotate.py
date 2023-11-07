# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import logging

import torch
from tqdm import tqdm

from .model_utils import (
    get_attention_inputs,
    get_attention_output,
    get_embeddings,
    get_first_layernorm,
    get_layer0_inputs,
    get_layers,
    get_lm_head,
    get_mlp_inputs,
    get_mlp_output,
    get_pre_head_layernorm,
    get_second_layernorm,
    get_signals,
)
from .utils import cleanup_memory, pca_calc
from .modules import RMSN

DEV = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def rotate_attention_inputs(layer, Q):
    # Rotate the WQ, WK and WV matrices of the self-attention layer.
    for W in get_attention_inputs(layer):
        dtype = W.weight.dtype
        W_ = W.weight.to(device=DEV, dtype=torch.float64)
        W.weight.data = torch.matmul(W_, Q).to(device="cpu", dtype=dtype)


def slice_attention_inputs(layer, new_embedding_dimension):
    # Slice the  WQ, WK and WV matrices of the self-attention layer.
    for W in get_attention_inputs(layer):
        W.weight.data = W.weight.data[:, :new_embedding_dimension]
        W.in_features = new_embedding_dimension

    layer.attn_shortcut_Q = layer.attn_shortcut_Q[:new_embedding_dimension, :]

    get_first_layernorm(layer).normalized_shape = (new_embedding_dimension,)


def rotate_attention_output(layer, Q):
    # Rotate output matrix of the self-attention layer.
    W = get_attention_output(layer)

    dtype = W.weight.data.dtype
    W_ = W.weight.data.to(device=DEV, dtype=torch.float64)
    W.weight.data = torch.matmul(Q.T, W_).to(device="cpu", dtype=dtype)
    if W.bias is not None:
        b = W.bias.data.to(device=DEV, dtype=torch.float64)
        W.bias.data = torch.matmul(Q.T, b).to(device="cpu", dtype=dtype)


def slice_attention_output(layer, new_embedding_dimension):
    # Slice output matrix of the self-attention layer.
    W = get_attention_output(layer)
    W.weight.data = W.weight.data[:new_embedding_dimension, :]
    if W.bias is not None:
        W.bias.data = W.bias.data[:new_embedding_dimension]
    W.out_features = new_embedding_dimension

    layer.attn_shortcut_Q = layer.attn_shortcut_Q[:, :new_embedding_dimension]
    if layer.attn_shortcut_bias is not None:
        layer.attn_shortcut_bias = layer.attn_shortcut_bias[:new_embedding_dimension]


def rotate_mlp_input(layer, Q):
    # Rotate the MLP input weights.
    for W in get_mlp_inputs(layer):
        dtype = W.weight.dtype
        W_ = W.weight.data.to(device=DEV, dtype=torch.float64)
        W.weight.data = torch.matmul(W_, Q).to(device="cpu", dtype=dtype)


def slice_mlp_input(layer, new_embedding_dimension):
    # Slice the MLP input weights.
    for W in get_mlp_inputs(layer):
        W.weight.data = W.weight.data[:, :new_embedding_dimension]
        W.in_features = new_embedding_dimension

    # slice shortcut
    layer.mlp_shortcut_Q = layer.mlp_shortcut_Q[:new_embedding_dimension, :]

    # modify layernorm
    get_second_layernorm(layer).normalized_shape = (new_embedding_dimension,)


def rotate_mlp_output(layer, Q):
    # Rotate the MLP output weights and bias.
    W = get_mlp_output(layer)
    dtype = W.weight.data.dtype
    W_ = W.weight.data.to(device=DEV, dtype=torch.float64)
    W.weight.data = torch.matmul(Q.T, W_).to(device="cpu", dtype=dtype)
    if W.bias is not None:
        b = W.bias.data.to(device=DEV, dtype=torch.float64)
        W.bias.data = torch.matmul(Q.T, b).to(device="cpu", dtype=dtype)


def slice_mlp_output(layer, new_embedding_dimension):
    # Slice the MLP output weights and bias.
    W = get_mlp_output(layer)
    W.weight.data = W.weight.data[:new_embedding_dimension, :]
    if W.bias is not None:
        W.bias.data = W.bias.data[:new_embedding_dimension]
    W.out_features = new_embedding_dimension

    layer.mlp_shortcut_Q = layer.mlp_shortcut_Q[:, :new_embedding_dimension]
    if layer.mlp_shortcut_bias is not None:
        layer.mlp_shortcut_bias = layer.mlp_shortcut_bias[:new_embedding_dimension]


def rotate_embeddings(model, Q):
    # Rotate the embeddings.
    for W in get_embeddings(model):
        dtype = W.weight.data.dtype
        W_ = W.weight.data.to(device=DEV, dtype=torch.float64)
        W.weight.data = torch.matmul(W_, Q).to(device="cpu", dtype=dtype)

    # Run GC and cleanup GPU memory
    cleanup_memory()


def slice_embeddings(model, new_embedding_dimension):
    # Slice the embeddings.
    for W in get_embeddings(model):
        W.weight.data = W.weight.data[:, :new_embedding_dimension]


def rotate_head(model, Q):
    # Rotate the head.
    W = get_lm_head(model)
    dtype = W.weight.data.dtype
    W_ = W.weight.data.to(device=DEV, dtype=torch.float64)
    W.weight.data = torch.matmul(W_, Q).to(device="cpu", dtype=dtype)


def slice_head(model, new_embedding_dimension):
    # Slice the head.
    model.lm_head.weight.data = model.lm_head.weight.data[:, :new_embedding_dimension]
    model.lm_head.in_features = new_embedding_dimension


@torch.no_grad()
def rotate_and_slice(model, dataloader, new_embedding_dimension, do_slice_head=False, fix_biases=False):
    """
    Rotate and slice a model, with interleaved slicing and PCA calculations
    """
    model.eval()
    dtype = next(iter(model.parameters())).dtype

    inps = []

    # Process the first batch separately to get the attention mask
    first_batch = next(iter(dataloader))
    inp, attention_mask = get_layer0_inputs(model, first_batch)
    inps.append(inp)

    # Process the remaining batches
    for batch in dataloader:
        inp, _ = get_layer0_inputs(model, batch)
        inps.append(inp)

    inps = torch.cat(inps)

    _, Q = pca_calc(inps.reshape(-1, model.config.hidden_size))
    Q = Q.to(device=DEV)

    rotate_embeddings(model, Q)
    slice_embeddings(model, new_embedding_dimension)

    # rotate and slice inputs
    new_inps = torch.matmul(inps, Q.to(dtype=dtype))[:, :, :new_embedding_dimension]
    rmsn = RMSN(Q.shape[-1])
    errors = inps - torch.matmul(new_inps, Q.T.to(dtype=dtype)[:new_embedding_dimension, :])
    shortcut_bias = torch.mean(errors, dim=[0, 1])
    # errors = rmsn(inps) - torch.matmul(rmsn(new_inps), Q.T.to(dtype=dtype)[:new_embedding_dimension, :])
    # input_bias = torch.mean(errors, dim=[0, 1])
    inps = new_inps

    logging.info("Rotate and slice layers")
    layers = get_layers(model)
    for layer in tqdm(layers, unit="layer", desc="Rotating and slicing"):
        layer.attn_shortcut_Q = Q.T.clone().to(dtype=dtype)
        if fix_biases:
            layer.attn_shortcut_bias = shortcut_bias.clone().to(dtype=dtype)

        # rotate and slice the attention inputs to match previous layer
        rotate_attention_inputs(layer, Q)
        slice_attention_inputs(layer, new_embedding_dimension)

        # get signal between attention and mlp, rotate and slice
        mlp_ln_inputs, _ = get_signals(layer, inps, attention_mask)
        _, Q = pca_calc(mlp_ln_inputs.reshape(-1, mlp_ln_inputs.shape[-1]))
        Q = Q.to(device=DEV, dtype=torch.float64)
        
        # compute bias correction
        mlp_ln_inputs = mlp_ln_inputs.to(device=Q.device)
        new_inputs = torch.matmul(mlp_ln_inputs, Q.to(dtype=dtype)[:, :new_embedding_dimension])
        errors = mlp_ln_inputs - torch.matmul(new_inputs, Q.T.to(dtype=dtype)[:new_embedding_dimension, :])
        shortcut_bias = torch.mean(errors, dim=[0]) # TODO: check this dim is correct wih Max's refactor? I expect it to be [0, 1]

        # apply new Q to attention output
        layer.attn_shortcut_Q = torch.matmul(layer.attn_shortcut_Q, Q.to(dtype=dtype))
        if fix_biases:
            layer.attn_shortcut_bias = torch.matmul(Q.T.to(dtype=dtype), layer.attn_shortcut_bias)
        rotate_attention_output(layer, Q)
        slice_attention_output(layer, new_embedding_dimension)

        # apply new Q to mlp input
        layer.mlp_shortcut_Q = Q.T.clone().to(dtype=dtype)
        if fix_biases:
            layer.mlp_shortcut_bias = shortcut_bias.clone().to(dtype=dtype)
        rotate_mlp_input(layer, Q)
        slice_mlp_input(layer, new_embedding_dimension)

        # Run GC and cleanup GPU memory
        cleanup_memory()

        # now compute the outputs of the layer with slicing between Attention and mlp.
        _, outputs = get_signals(layer, inps, attention_mask)
        _, Q = pca_calc(outputs.reshape(-1, outputs.shape[-1]))
        
        layer.mlp_shortcut_Q = torch.matmul(layer.mlp_shortcut_Q, Q.to(dtype=dtype))
        if fix_biases:
            layer.mlp_shortcut_bias = torch.matmul(Q.T.to(dtype=dtype), layer.mlp_shortcut_bias)

        # optionally slice the mlp/head connection in the last layer
        dim = new_embedding_dimension
        if layer is layers[-1]:
            if not do_slice_head:
                dim = model.config.hidden_size

        rotate_mlp_output(layer, Q)
        slice_mlp_output(layer, dim)

        inps = torch.matmul(outputs, Q.to(dtype=dtype))[:, :, :dim]
        errors = outputs - torch.matmul(inps, Q.T.to(dtype=dtype)[:dim, :])
        shortcut_bias = torch.mean(errors, dim=[0, 1]) 

        layer = layer.to('cpu')

        # Run GC and cleanup GPU memory
        cleanup_memory()

    # rotate and slice head
    rotate_head(model, Q)
    
    if do_slice_head:
        slice_head(model, new_embedding_dimension)
        
        # a final chorcut bias added to the head.
        head = get_lm_head(model)
        if head.bias is None:
            head.bias = torch.nn.Parameter(shortcut_bias)
        else:
            head.bias = torch.nn.Parameter(head.bias + shortcut_bias)

    logging.info("Rotate and slice layers done")


@torch.no_grad()
def rotate(model, dataloader):
    """
    Rotate a model.
    """
    model.eval()
    dtype = next(iter(model.parameters())).dtype  # Get the dtype of the model.

    # List of layers to rotate.
    layers = get_layers(model)

    # Get the input of the first layer norm and calculate the Q_1
    inps, attention_mask = get_layer0_inputs(model, dataloader)
    _, Q_1 = pca_calc(inps.reshape(-1, model.config.hidden_size))
    Q_1 = Q_1.to(device=DEV)

    # Rotate the embeddings.
    rotate_embeddings(model, Q_1)

    # Rotate the rest of the model.
    logging.info("Rotate layers")
    for layer in tqdm(layers, unit="layer", desc="Rotating"):
        # Extract the inputs and outputs of the second layernorm input and calculate the Q_3
        mlp_ln_inputs, outs = get_signals(layer, inps, attention_mask)
        _, Q_3 = pca_calc(mlp_ln_inputs.reshape(-1, mlp_ln_inputs.shape[-1]))
        Q_3 = Q_3.to(device=DEV)
        _, Q_5 = pca_calc(outs.reshape(-1, outs.shape[-1]))
        Q_5 = Q_5.to(device=DEV)

        # Rotate the Q, K and V matrices of the self-attention layer.
        rotate_attention_inputs(layer, Q_1)

        # Set the shortcut rotation matrix of the self-attention layer.
        layer.attn_shortcut_Q = torch.matmul(Q_1.clone().T, Q_3.clone()).to(device="cpu", dtype=dtype)

        # Rotate the Attention output matrix
        rotate_attention_output(layer, Q_3)

        # Rotate the MLP input
        rotate_mlp_input(layer, Q_3)

        # Set the shortcut rotation matrix of the MLP.
        layer.mlp_shortcut_Q = torch.matmul(Q_3.clone().T, Q_5.clone()).to(device="cpu", dtype=dtype)

        # Rotate MLP output
        rotate_mlp_output(layer, Q_5)

        # Run GC and cleanup GPU memory
        cleanup_memory()

        inps = outs  # The inputs to the next layer are the outputs from this one!
        Q_1 = Q_5  # first rotation in the next layer is the last one in this...

    rotate_head(model, Q_5)
    logging.info("Rotate layers done")


def slice_rotated_model(model, new_embedding_dimension, do_slice_head=False):
    model.eval()

    # slice embeddings
    slice_embeddings(model, new_embedding_dimension)

    # List of layers to sice.
    layers = get_layers(model)

    for layer in layers:
        slice_attention_inputs(layer, new_embedding_dimension)
        slice_attention_output(layer, new_embedding_dimension)

        # Slice attention shortcut matrix
        layer.attn_shortcut_Q = layer.attn_shortcut_Q[:new_embedding_dimension, :new_embedding_dimension]

        slice_mlp_input(layer, new_embedding_dimension)

        # optionally slice the mlp/head connection in the last layer
        dim = new_embedding_dimension
        if layer is layers[-1]:
            if not do_slice_head:
                dim = model.config.hidden_size

        slice_mlp_output(layer, dim)
        layer.mlp_shortcut_Q = layer.mlp_shortcut_Q[:new_embedding_dimension, :dim]

    if do_slice_head:
        get_pre_head_layernorm(model).normalized_shape = (new_embedding_dimension,)
        slice_head(model, new_embedding_dimension)
