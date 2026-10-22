"""Disposable, time-limited regex validation and compilation subprocess.

Only patterns provably consuming on every branch are accepted. Lookarounds,
backreferences, inline flags, fuzzy matching, nested sets, and other constructs
outside the supported grammar are rejected even when a particular use is safe.
Flags in the rule's flags field remain supported.
"""
import json
import pickle
import sys
import regex


class UnsupportedRegex(ValueError):
    pass


def consumes_on_every_branch(pattern):
    """Prove a consuming match for a deliberately limited regex grammar.

    Constructs whose semantics cannot be established here are rejected, rather
    than relying on examples to prove the absence of zero-width matches.
    """
    index = 0

    def expression(grouped=False):
        nonlocal index
        branches = []
        consumes = False
        while index < len(pattern) and pattern[index] != ')':
            char = pattern[index]
            if char == '|':
                branches.append(consumes)
                consumes = False
                index += 1
                continue
            index += 1
            if char == '(':
                if pattern.startswith('?:', index):
                    index += 2
                elif index < len(pattern) and pattern[index] in '?*':
                    raise UnsupportedRegex()
                atom = expression(True)
            elif char == '[':
                if index < len(pattern) and pattern[index] == '^':
                    index += 1
                count = 0
                while index < len(pattern) and pattern[index] != ']':
                    if pattern[index] == '[':
                        raise UnsupportedRegex()
                    if pattern[index] == '\\':
                        index += 1
                        if index >= len(pattern) or (pattern[index].isalnum() and pattern[index] not in 'dDsSwWb'):
                            raise UnsupportedRegex()
                    index += 1
                    count += 1
                if not count or index == len(pattern):
                    raise UnsupportedRegex()
                index += 1
                atom = True
            elif char == '\\':
                if index == len(pattern):
                    raise UnsupportedRegex()
                escaped = pattern[index]
                index += 1
                if escaped in 'AbBZG':
                    atom = False
                elif escaped in 'dDsSwWXR' or not escaped.isalnum():
                    atom = True
                else:
                    raise UnsupportedRegex()
            elif char in '^$':
                atom = False
            elif char in '*+?{}':
                raise UnsupportedRegex()
            else:
                atom = True

            if index < len(pattern) and pattern[index] in '*+?{':
                quantifier = pattern[index]
                index += 1
                if quantifier == '{':
                    start = index
                    while index < len(pattern) and pattern[index].isascii() and pattern[index].isdigit():
                        index += 1
                    if index == start:
                        raise UnsupportedRegex()
                    minimum = int(pattern[start:index])
                    if index < len(pattern) and pattern[index] == ',':
                        index += 1
                        while index < len(pattern) and pattern[index].isascii() and pattern[index].isdigit():
                            index += 1
                    if index == len(pattern) or pattern[index] != '}':
                        raise UnsupportedRegex()
                    index += 1
                    atom = atom and minimum > 0
                elif quantifier in '*?':
                    atom = False
                if index < len(pattern) and pattern[index] in '?+':
                    index += 1
            consumes = consumes or atom
        branches.append(consumes)
        if grouped:
            if index == len(pattern):
                raise UnsupportedRegex()
            index += 1
        elif index != len(pattern):
            raise UnsupportedRegex()
        return all(branches)

    return expression()


def main():
    try:
        compiled = []
        for matcher in json.load(sys.stdin):
            flags = regex.VERSION1
            for name in matcher['flags']:
                flags |= getattr(regex, name)
            pattern = regex.compile(matcher['pattern'], flags)
            if not consumes_on_every_branch(matcher['pattern']):
                return 2
            compiled.append(pattern)
        sys.stdout.buffer.write(pickle.dumps(compiled, protocol=4))
    except UnsupportedRegex:
        return 3
    except (regex.error, ValueError, OverflowError, RuntimeError, RecursionError):
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
