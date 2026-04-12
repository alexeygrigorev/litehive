from litehive.cli.parsers.common import add_workspace_argument


def register_attention_parser(subparsers):
    parser = subparsers.add_parser("attention", help="List and resolve operator-attention items")
    add_workspace_argument(parser)
    attention_subparsers = parser.add_subparsers(dest="attention_command")

    attention_subparsers.add_parser("list", help="Show unresolved operator-attention items")

    resolve_parser = attention_subparsers.add_parser("resolve", help="Resolve one operator-attention item")
    resolve_parser.add_argument("attention_id", type=int, help="Stable attention item id")
