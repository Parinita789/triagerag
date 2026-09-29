from triagerag.index.jira import load_all

if __name__ == "__main__":
    n_tickets, n_links = load_all()
    print(f"loaded {n_tickets:,} tickets, {n_links:,} links")