#!/usr/bin/env python3
import unittest

from discover import (
    allowed_directory,
    canonicalize,
    classify_path,
    extract_links,
    parse_letters,
)


class TestDiscover(unittest.TestCase):
    def test_parse_letters(self):
        self.assertEqual(parse_letters("a"), {"a"})
        self.assertEqual(parse_letters("a-c"), {"a", "b", "c"})

    def test_classify_and_allow(self):
        person = "https://www.truepeoplesearch.com/find/person/px82l44nur68u2l2l8n60"
        adams = "https://www.truepeoplesearch.com/find/adams"
        brown = "https://www.truepeoplesearch.com/find/brown"
        letter = "https://www.truepeoplesearch.com/find/a"
        self.assertEqual(classify_path(person), "person")
        self.assertEqual(classify_path(adams), "directory")
        self.assertTrue(allowed_directory(adams, {"a"}))
        self.assertTrue(allowed_directory(letter, {"a"}))
        self.assertFalse(allowed_directory(brown, {"a"}))

    def test_extract_links(self):
        html = """
        <a href="/find/adams">Adams</a>
        <a href="/find/brown">Brown</a>
        <a href="/find/person/abc123def">John Adams</a>
        <a href="https://www.truepeoplesearch.com/find/person/xyz789">Jane</a>
        <a href="/find/b">B</a>
        <a href="/about">About</a>
        """
        base = "https://www.truepeoplesearch.com/find/a"
        persons, dirs = extract_links(html, base)
        self.assertEqual(len(persons), 2)
        self.assertTrue(any(u.endswith("/find/person/abc123def") for u in persons))
        self.assertTrue(any("/find/adams" in u for u in dirs))
        self.assertTrue(any(u.endswith("/find/b") for u in dirs))
        dirs_a = [u for u in dirs if allowed_directory(u, {"a"})]
        self.assertTrue(all("/find/b" not in u for u in dirs_a))
        self.assertEqual(canonicalize("/find/person/abc123def"), persons[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
