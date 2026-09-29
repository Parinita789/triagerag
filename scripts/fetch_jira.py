from triagerag.index.jira import fetch_all

if __name__ == "__main__":
    total = fetch_all()
    print(f"\ndone — {total} tickets on disk")