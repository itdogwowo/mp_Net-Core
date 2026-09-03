"""
This example demonstrates how to use the jsffi module to convert a Python dictionary into a
JavaScript object and then stringify it using JSON.stringify in JavaScript.
The resulting JSON string is printed to the console.
"""

from js import window
from jsffi import to_js

json_stringify = window.eval("(function(a) { return JSON.stringify(a); })")

dump = json_stringify(to_js({
    "glossary": {
        "title": "example glossary",
		"GlossDiv": {
            "title": "S",
			"GlossList": {
                "GlossEntry": {
                    "ID": "SGML",
					"SortAs": "SGML",
					"GlossTerm": "Standard Generalized Markup Language",
					"Acronym": "SGML",
					"Abbrev": "ISO 8879:1986",
					"GlossDef": {
                        "para": "A meta-markup language, used to create markup languages such as DocBook.",
						"GlossSeeAlso": ["GML", "XML"]
                    },
					"GlossSee": "markup"
                }
            }
        }
    }
}))

print(dump)
