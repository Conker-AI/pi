"""Bounded literal-match previews retain offsets in the original Unicode text."""


def excerpt(text, query, *, width=400, leading=80):
    folded_position = text.casefold().find(query.casefold())
    position = max(0, folded_position)
    if folded_position > 0 and not text.isascii():
        folded_offset = 0
        for index, character in enumerate(text):
            folded_offset += len(character.casefold())
            if folded_offset > folded_position:
                position = index
                break
    start = max(0, position - leading)
    return text[start : start + width]
