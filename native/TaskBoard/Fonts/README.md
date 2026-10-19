# Noto Sans SC

Source: https://github.com/google/fonts/tree/main/ofl/notosanssc
License: SIL Open Font License 1.1; see OFL.txt.

The bundled TrueType fonts are static weight-400 and weight-700 instances of NotoSansSC[wght].ttf, generated with fontTools 4.61.1:

```python
for weight, name in [(400, "Regular"), (700, "Bold")]:
    font = TTFont("NotoSansSC-variable.ttf")
    font = instantiateVariableFont(font, {"wght": weight}, inplace=True, optimize=True, updateFontNames=True)
    font.save(f"NotoSansSC-{name}.ttf")
```

Source SHA-256: `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`
