from pathlib import Path
from loginsights.ingest.loader import load_labels

if __name__ == "__main__":
    count = load_labels(Path("data/preprocessed/anomaly_label.csv"))
    print(f"inserted {count:,} blocks")