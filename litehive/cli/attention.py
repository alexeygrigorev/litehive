from litehive.attention import list_attention, resolve_attention


def cmd_attention_list(args) -> int:
    items = list_attention(args.workspace)
    print(f"pending_attention: {len(items)}")
    if not items:
        print("attention: none")
        return 0
    for item in items:
        task_label = item.task_id or "-"
        print(
            f"attention: {item.id} created_at={item.created_at} kind={item.kind} "
            f"task_id={task_label} title={item.title}"
        )
        print(f"  reason: {item.reason}")
        print(f"  suggested_action: {item.suggested_action}")
    return 0


def cmd_attention_resolve(args) -> int:
    item = resolve_attention(args.workspace, args.attention_id)
    if item is None:
        print(f"attention item not found: {args.attention_id}")
        return 1
    print(f"resolved_attention: {item.id}")
    print(f"title: {item.title}")
    return 0
