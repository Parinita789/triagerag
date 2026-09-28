from pathlib import Path
from loginsights.ingest.loader import load_log_lines

if __name__ == "__main__":
    count = load_log_lines(Path("data/HDFS.log"))
    print(f"total lines inserted: {count:,}")