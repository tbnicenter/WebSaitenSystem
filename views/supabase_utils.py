def fetch_all_pages(query_factory, page_size=1000):
    rows = []
    page_start = 0
    while True:
        response = query_factory(page_start, page_start + page_size - 1).execute()
        page_data = response.data or []
        if not page_data:
            return rows
        rows.extend(page_data)
        page_start += len(page_data)
