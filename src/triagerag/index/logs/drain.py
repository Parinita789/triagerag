from pathlib import Path

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.template_miner_config import TemplateMinerConfig
from drain3.masking import MaskingInstruction

_IP = r"\d+\.\d+\.\d+\.\d+(?::\d+)?"

DEFAULT_STATE_PATH = Path(__file__).resolve().parents[4] / "models" / "drain_state.bin"


def build_miner(state_path: Path | None = None) -> TemplateMiner:
    """Build a miner. With state_path, existing state is loaded from it and
    snapshots are written back to it as training progresses."""
    config = TemplateMinerConfig()
    config.drain_depth = 4
    config.drain_max_children = 100
    config.parametrize_numeric_tokens = True
    # Order matters: masks are applied sequentially.
    config.masking_instructions = [
        MaskingInstruction(r"\d+\.\d+\.\d+\.\d+(:\d+)?", "IP"),     # 1. IPs first
        MaskingInstruction(r"blk_-?\d+", "BLK"),                    # 2. block IDs
        MaskingInstruction(r"java[\w.$]*(Exception|Error)[^\n]*", "EXC"),  # 3. exception + rest of line
        MaskingInstruction(r"(/[\w.\-]+)+", "PATH"),                # 4. paths, after IPs
        MaskingInstruction(r"\b\d+\b", "NUM"),                      # 5. numbers last
    ]
    config.drain_sim_th = 0.85
    persistence = FilePersistence(str(state_path)) if state_path else None
    return TemplateMiner(persistence_handler=persistence, config=config)


def load_miner(state_path: Path = DEFAULT_STATE_PATH) -> TemplateMiner:
    """Load a trained miner. Use miner.match(msg) for inference: unlike
    add_log_message, it never creates or mutates templates."""
    if not state_path.exists():
        raise FileNotFoundError(
            f"no Drain state at {state_path}; run scripts/train_drain.py first"
        )
    return build_miner(state_path)
