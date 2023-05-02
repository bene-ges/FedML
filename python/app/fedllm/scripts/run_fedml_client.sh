#!/usr/bin/env bash
set -e

BASE_DIR="$(dirname "$0")"
BASE_DIR="$(realpath "${BASE_DIR}/../")"
cd "${BASE_DIR}"

export WANDB_MODE=disabled # remove this line if you want to use wandb

# FedML related
RANK="$1"
RUN_ID="$2"

# GPU cluster related
TORCH_DISTRIBUTED_DEFAULT_PORT=29500
MASTER_ADDR="${3:-"localhost"}"
MASTER_PORT="${4:-$((TORCH_DISTRIBUTED_DEFAULT_PORT + RANK))}"
NUM_GPU="$(python -c "import torch; print(torch.cuda.device_count())")"
NUM_NODES="${5:-1}"

# FedML config
CONFIG_PATH="fedml_config/fedml_config.yaml"

deepspeed \
  --num_nodes="${NUM_NODES}" \
  --num_gpus="${NUM_GPU}" \
  --master_addr="${MASTER_ADDR}" \
  --master_port="${MASTER_PORT}" \
  main_federated_llm.py \
  --cf "${CONFIG_PATH}" \
  --rank "${RANK}" \
  --role client \
  --run_id "${RUN_ID}"
