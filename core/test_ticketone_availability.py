from api.scrapers.ticketone.ticketone_availability_debug import inspect_ticketone_list_page
from api.scrapers.ticketone.ticketone_availability import check_ticketone_list_availability


def run_test():
    rows = inspect_ticketone_list_page(limit=20, verbose=False)

    for idx, row in enumerate(rows, start=1):
        result = check_ticketone_list_availability(row["container_text"])

        print("=" * 100)
        print("N:", idx)
        print("TITLE:", row["title"])
        print("URL:", row["url"])
        print("STATUS:", result["status"])
        print("AVAILABLE:", result["available"])
        print("REASON:", result["reason"])
        print("TEXT:", row["container_text"][:200])


if __name__ == "__main__":
    run_test()