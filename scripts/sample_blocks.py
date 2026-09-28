from loginsights.ingest.loader import sample_blocks

if __name__ == "__main__":
    count = sample_blocks(n=5000, seed=42)
    print(f"sampled {count:,} blocks")