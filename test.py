import tree_sitter_python as tspython
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

JAVA_LANGUAGE = Language(tsjava.language())
parser_java = Parser(JAVA_LANGUAGE)

code = b"import os\nfrom pathlib import Path\nprint('hello')\n"
tree = parser.parse(code)

code_java = b"import java.util.List;\npublic class HelloWorld {\n    public static void main(String[] args) {\n        System.out.println(\"Hello, World!\");\n    }\n}\n"
tree_java = parser_java.parse(code_java)

print(tree.root_node)
print()
print(tree.root_node.children)
print()
print(tree.root_node.children[0].type)        # 'import_statement'
print()
print(tree.root_node.children[0].text)        # 'import_statement'
print()
print(tree.root_node.children[0].children)    # the actual tokens
print()

print(tree_java.root_node)
print()
print(tree_java.root_node.children)
print()
print(tree_java.root_node.children[0].type)        # 'import_declaration'
print()
print(tree_java.root_node.children[0].text)        # 'import_declaration'
print()
print(tree_java.root_node.children[0].children)    # the actual tokens
