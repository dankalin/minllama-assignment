from contextlib import nullcontext
from typing import Optional, Tuple
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

from base_llama import LlamaPreTrainedModel, LlamaConfig
from rope import apply_rotary_emb
from utils import *

# Root Mean Square Layer Normalization (https://arxiv.org/abs/1910.07467)
# borrowed from the official Llama implementation:
# https://github.com/facebookresearch/llama/blob/main/llama/model.py
class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        """
        Initialize the RMSNorm normalization layer.

        Args:
            dim (int): The dimension of the input tensor.
            eps (float, optional): A small value added to the denominator for numerical stability. Default is 1e-6.

        Attributes:
            eps (float): A small value added to the denominator for numerical stability.
            weight (nn.Parameter): Learnable scaling parameter.

        """
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        """
        Compute the root mean square normalization.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The normalized tensor.
        """
        # Compute the squared norm of the input tensor
        squared_norm = torch.mean(x ** 2, dim=-1, keepdim=True)

        # Add the epsilon value to the squared norm for numerical stability
        squared_norm_eps = squared_norm + self.eps

        # Compute the root mean square (RMS) by taking the square root
        rms = torch.sqrt(squared_norm_eps)

        # Normalize the input tensor by dividing it by the RMS
        normalized_x = x / rms

        return normalized_x

    def forward(self, x):
        """
        Apply the root mean square normalizer.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor after applying RMSNorm.

        """
        output = self._norm(x.float()).type_as(x)
        return output * self.weight

class Attention(nn.Module):
    def __init__(self, config: LlamaConfig):
        super().__init__()
        self.n_kv_heads = config.n_heads if config.n_kv_heads is None else config.n_kv_heads
        assert config.n_heads % self.n_kv_heads == 0
        model_parallel_size = 1
        self.n_local_heads = config.n_heads // model_parallel_size
        self.n_local_kv_heads = self.n_kv_heads // model_parallel_size
        self.n_rep = self.n_local_heads // self.n_local_kv_heads
        self.head_dim = config.dim // config.n_heads
        self.max_seq_len = config.max_seq_len
        self.compute_query = nn.Linear(config.dim, config.n_heads * self.head_dim, bias=False)
        self.compute_key = nn.Linear(config.dim, self.n_kv_heads * self.head_dim, bias=False)
        self.compute_value = nn.Linear(config.dim, self.n_kv_heads * self.head_dim, bias=False)
        self.compute_output = nn.Linear(config.n_heads * self.head_dim, config.dim, bias=False)
        self.attn_dropout = nn.Dropout(config.dropout)
        self.resid_dropout = nn.Dropout(config.dropout)
        self.dropout = config.dropout

    def compute_query_key_value_scores(self,
                                       query: torch.Tensor,
                                       key: torch.Tensor,
                                       value: torch.Tensor) -> torch.Tensor:
        scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(self.head_dim)
        # scores shape: (bs, n_local_heads, seqlen)

        # compute attention probabilities
        probs = torch.softmax(scores, dim=-1)
        # probs shape: (bs, n_local_heads, seqlen)

        output = torch.matmul(probs, value)
        # output shape: (bs, n_local_heads, seqlen, head_dim)

        return output

    def forward(
        self,
        x: torch.Tensor
    ):
        '''
        Llama2 uses Grouped-Query Attention. The details of GQA are actually
        not critical to solving this assignment; you are simply asked to
        compute Scaled Dot Product Attention (see above for details). GQA is
        a memory optimization to compute multi-head attention efficiently. See
        Section 2.2 in https://arxiv.org/abs/2305.13245 or
        https://ai.plainenglish.io/understanding-llama2-kv-cache-grouped-query-attention-rotary-embedding-and-more-c17e5f49a6d7
        for details.
        '''
        batch_size, seqlen, _ = x.shape

        query = self.compute_query(x)
        key = self.compute_key(x)
        value = self.compute_value(x)
        query = query.view(batch_size, seqlen, self.n_local_heads, self.head_dim)
        key = key.view(batch_size, seqlen, self.n_local_kv_heads, self.head_dim)
        value = value.view(batch_size, seqlen, self.n_local_kv_heads, self.head_dim)

        # RoPE relative positional embeddings
        query, key = apply_rotary_emb(query, key, self.head_dim, self.max_seq_len)

        # Grouped multiquery attention: expand out keys and values.
        # Convert both to:
        # (bs, seqlen, n_local_heads, head_dim)
        key = torch.repeat_interleave(key, dim=2, repeats=self.n_rep)
        value = torch.repeat_interleave(value, dim=2, repeats=self.n_rep)

        # make heads into a batch dimension
        query = query.transpose(1, 2)  # (bs, n_local_heads, seqlen, head_dim)
        key = key.transpose(1, 2)
        value = value.transpose(1, 2)
        output = self.compute_query_key_value_scores(query, key, value)

        # restore time as batch dimension and concat heads
        output = output.transpose(1, 2).contiguous().view(batch_size, seqlen, -1)

        # final projection into the residual stream
        output = self.resid_dropout(self.compute_output(output))
        return output


class FeedForward(nn.Module):
    def __init__(self, dim: int, hidden_dim: int, multiple_of: int, dropout: float):
        super().__init__()
        if hidden_dim is None:
            hidden_dim = 4 * dim
            hidden_dim = int(2 * hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)
        self.w1 = nn.Linear(dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, dim, bias=False)
        self.w3 = nn.Linear(dim, hidden_dim, bias=False)
        self.dropout = nn.Dropout(dropout)

    def SwiGLU(self, x: torch.Tensor) -> torch.Tensor:
        '''
        Compute the SwiGLU activation function (see Section 2 in
        https://arxiv.org/abs/2204.02311
        '''
        return F.silu(self.w1(x)) * self.w3(x)

    def forward(self, x):
        return self.dropout(self.w2(self.SwiGLU(x)))


class LlamaLayer(nn.Module):
    def __init__(self, layer_id: int, config: LlamaConfig):
        super().__init__()
        self.n_heads = config.n_heads
        self.dim = config.dim
        self.head_dim = config.dim // config.n_heads
        self.attention = Attention(config)
        self.feed_forward = FeedForward(
            dim=config.dim,
            hidden_dim=config.hidden_dim,
            multiple_of=config.multiple_of,
            dropout=config.dropout,
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(config.dim, eps=config.layer_norm_eps)
        self.ffn_norm = RMSNorm(config.dim, eps=config.layer_norm_eps)

    def forward(self, x):
        """
        This is the forward pass of the basic transformer building block.

        Args:
            x (torch.Tensor): The input tensor.

        Returns:
            torch.Tensor: The output tensor after applying the transformer block.
        """
        # Step 1: Layer normalization of the input
        x_norm = self.attention_norm(x)

        # Step 2: Self-attention on the layer-normalized input
        attention_output = self.attention(x_norm)

        # Step 3: Residual connection (add the input to the output of the self-attention)
        x = x + attention_output

        # Step 4: Layer normalization on the output of the self-attention
        x_norm = self.ffn_norm(x)

        # Step 5: Feed-forward network on the layer-normalized output of the self-attention
        ffn_output = self.feed_forward(x_norm)

        # Step 6: Residual connection (add the unnormalized self-attention output to the output of the feed-forward network)
        x = x + ffn_output

        return x

class Llama(LlamaPreTrainedModel):
    def __init__(self, config: LlamaConfig):
        '''
        You will probably never need to call this function, unless you decide
        to pretrain a Llama model from scratch.
        '''
        super().__init__(config)
        self.params = config
        self.vocab_size = config.vocab_size
        self.n_layers = config.n_layers

        self.tok_embeddings = nn.Embedding(config.vocab_size, config.dim)
        self.dropout = nn.Dropout(config.dropout)
        self.layers = torch.nn.ModuleList()
        for layer_id in range(config.n_layers):
            self.layers.append(LlamaLayer(layer_id, config))
        self.norm = RMSNorm(config.dim, eps=config.layer_norm_eps)
        self.output = nn.Linear(config.dim, config.vocab_size, bias=False)

        # share the unembedding parameters with the embedding parameters
        self.tok_embeddings.weight = self.output.weight # https://paperswithcode.com/method/weight-tying

        # some useful precompute for the RoPE relative positional embeddings

        # init all weights
        self.apply(self._init_weights)
        # apply special scaled init to the residual projections, per GPT-2 paper
        for pn, p in self.named_parameters():
            if pn.endswith('w3.weight') or pn.endswith('compute_output.weight'):
                torch.nn.init.normal_(p, mean=0.0, std=0.02/math.sqrt(2 * config.n_layers))

    def _init_weights(self, module):
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, tokens: torch.Tensor, targets: Optional[torch.Tensor] = None) -> torch.Tensor:
        _batch_size, seqlen = tokens.shape
        h = self.tok_embeddings(tokens)
        h = self.dropout(h)

        for layer in self.layers:
            h = layer(h)
        h = self.norm(h)

        if targets is not None:
            # if we are given some desired targets also calculate the loss
            logits = self.output(h)
        else:
            # inference-time mini-optimization: only forward the output on the very last position
            logits = self.output(h[:, [-1], :]) # note: using list [-1] to preserve the time dim

        return logits, h

    @torch.inference_mode()
    def generate(self, idx, max_new_tokens, temperature=1.0):
        """
        Take a conditioning sequence of indices idx (LongTensor of shape (b,t)) and complete
        the sequence max_new_tokens times, feeding the predictions back into the model each time.
        We perform this generation using basic temperature sampling.
        """
        for _ in range(max_new_tokens):
            # if the sequence context is growing too long we must crop it at block_size
            idx_cond = idx if idx.size(1) <= self.params.max_seq_len else idx[:, -self.params.max_seq_len:]
            # forward the model to get the logits for the index in the sequence
            logits, _ = self(idx_cond)
            logits = logits[:, -1, :]  # crop to just the final time step

            if temperature == 0.0:
                # select the single most likely index
                idx_next = logits.argmax(dim=-1, keepdim=True)
            else:
                # Step 1: Identify the logits at the final step
                logits = logits[:, -1, :]

                # Step 2: Scale the logits by the given temperature
                scaled_logits = logits / temperature

                # Step 3: Normalize the scaled logits with a softmax to obtain scaled probabilities
                scaled_probs = torch.softmax(scaled_logits, dim=-1)

                # Step 4: Sample from the scaled probability distribution
                idx_next = torch.multinomial(scaled_probs, num_samples=1)

            # append sampled index to the running sequence and continue
            idx = torch.cat((idx, idx_next), dim=1)

        return idx

def load_pretrained(checkpoint):
  device = 'cuda' if torch.cuda.is_available() else 'cpu' # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1', etc.
  #dtype = 'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16' # 'float32' or 'bfloat16' or 'float16'
  dtype = "float32"

  torch.backends.cuda.matmul.allow_tf32 = True # allow tf32 on matmul
  torch.backends.cudnn.allow_tf32 = True # allow tf32 on cudnn
  device_type = 'cuda' if 'cuda' in device else 'cpu' # for later use in torch.autocast
  ptdtype = {'float32': torch.float32, 'bfloat16': torch.bfloat16, 'float16': torch.float16}[dtype]
  ctx = nullcontext() if device_type == 'cpu' else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

  # init from a model saved in a specific directory
  checkpoint_dict = torch.load(checkpoint, map_location=device)
  config = LlamaConfig(**checkpoint_dict['model_args'])
  model = Llama(config)
  state_dict = checkpoint_dict['model']
  unwanted_prefix = '_orig_mod.'
  for k,v in list(state_dict.items()):
      if k.startswith(unwanted_prefix):
          state_dict[k[len(unwanted_prefix):]] = state_dict.pop(k)
  model.load_state_dict(state_dict, strict=False)
  return model
