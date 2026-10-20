"""Regenerate original presentation SVGs locally; no fonts, network or imaging dependencies."""

from pathlib import Path
import xml.etree.ElementTree as ET

ASSETS = Path(__file__).resolve().parent / "assets"


def write(name, body, title, description, height):
    text = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{height}" viewBox="0 0 1280 {height}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title><desc id="desc">{description}</desc>
{body}
</svg>
'''
    ET.fromstring(text)
    (ASSETS / name).write_text(text, encoding="utf-8", newline="\n")


def main():
    logo = ET.parse(ASSETS / "lab-wordmark.svg").getroot()
    logo.set("x", "64")
    logo.set("y", "36")
    logo.set("width", "670")
    logo.set("height", "83")
    mark = ET.tostring(logo, encoding="unicode")
    hero = '''<rect width="1280" height="640" fill="#011F5B"/>
<rect width="1280" height="154" fill="#ffffff"/>
<rect y="154" width="1280" height="6" fill="#990000"/>
<text x="1216" y="82" text-anchor="end" font-family="Arial,sans-serif" font-size="14" letter-spacing="2" fill="#011F5B">RESEARCH SOFTWARE</text>
<g font-family="Arial,sans-serif">
<text x="64" y="227" fill="#B9CDEB" font-size="15" letter-spacing="3">SCRIPT-FIRST PCB INTEROPERABILITY</text>
<text x="64" y="328" fill="#ffffff" font-size="77" font-family="Georgia,serif">Fusion &#8644; Quilter</text>
<text x="67" y="388" fill="#ffffff" font-size="32" font-family="Georgia,serif">Keep your design. Change your layout.</text>
<text x="67" y="438" fill="#B9CDEB" font-size="20">EAGLE to KiCad. Verified geometry. A path back.</text>
<rect x="878" y="211" width="337" height="263" rx="22" fill="#0B316B" stroke="#4B6A98"/>
<g stroke="#254D82" stroke-width="1">
<path d="M898 235H1195 M898 270H1195 M898 305H1195 M898 340H1195 M898 375H1195 M898 410H1195 M928 231V453 M968 231V453 M1008 231V453 M1048 231V453 M1088 231V453 M1128 231V453 M1168 231V453"/>
</g>
<g fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="5">
<path d="M920 260H976L1018 302H1073V386H1166" stroke="#ffffff"/>
<path d="M920 410H966L1008 368V324H1126V261H1174" stroke="#70BCE0"/>
<path d="M943 306H972V441H1142V410" stroke="#CC7E89"/>
</g>
<g fill="#011F5B" stroke="#ffffff" stroke-width="4">
<circle cx="920" cy="260" r="9"/><circle cx="1166" cy="386" r="9"/>
<circle cx="920" cy="410" r="9"/><circle cx="1174" cy="261" r="9"/>
<circle cx="943" cy="306" r="9"/><circle cx="1142" cy="410" r="9"/>
</g>
<rect x="1028" y="301" width="67" height="64" rx="7" fill="#011F5B" stroke="#B9CDEB" stroke-width="2"/>
<text x="1061" y="340" text-anchor="middle" fill="#ffffff" font-size="15" letter-spacing="2">PCB</text>
<rect x="64" y="518" width="1152" height="82" rx="12" fill="#082B63" stroke="#365783"/>
<text x="93" y="550" fill="#ffffff" font-size="21">01 / Preserve</text>
<text x="93" y="578" fill="#B9CDEB" font-size="16">Your schematic remains authoritative.</text>
<text x="473" y="550" fill="#ffffff" font-size="21">02 / Translate</text>
<text x="473" y="578" fill="#B9CDEB" font-size="16">Local conversion. Official KiCad engine.</text>
<text x="866" y="550" fill="#ffffff" font-size="21">03 / Verify</text>
<text x="866" y="578" fill="#B9CDEB" font-size="16">Compare geometry, not appearances.</text>
</g>'''
    write("hero.svg", hero + mark, "Fusion Quilter Bridge",
          "Penn Vet Van Eps Laboratory. Keep your design. Change your layout. Research software, not board certification.", 640)
    workflow = '''<rect width="1280" height="310" rx="18" fill="#F4F6FA"/>
<g font-family="Arial,sans-serif">
<text x="42" y="44" font-size="13" letter-spacing="2" fill="#52637B">THE ROUND TRIP / LOCAL CONVERSION, DELIBERATE CLOUD HANDOFF</text>
<g fill="#ffffff" stroke="#D3DBE7" stroke-width="2">
<rect x="42" y="76" width="308" height="112" rx="12"/>
<rect x="486" y="76" width="308" height="112" rx="12"/>
<rect x="930" y="76" width="308" height="112" rx="12"/>
</g>
<g fill="#011F5B" text-anchor="middle">
<text x="196" y="119" font-family="Georgia,serif" font-size="31">Fusion Electronics</text>
<text x="640" y="119" font-family="Georgia,serif" font-size="31">KiCad 9</text>
<text x="1084" y="119" font-family="Georgia,serif" font-size="31">Quilter</text>
<text x="196" y="154" font-size="16">Source schematic + PCB</text>
<text x="640" y="154" font-size="16">Verified interchange files</text>
<text x="1084" y="154" font-size="16">AI placement + routing</text>
</g>
<path d="M362 122H474 M450 110L474 122L450 134 M806 122H918 M894 110L918 122L894 134" fill="none" stroke="#011F5B" stroke-width="3"/>
<g text-anchor="middle" font-size="13" fill="#52637B">
<text x="418" y="94">CONVERT</text><text x="862" y="94">UPLOAD</text>
</g>
<path d="M1084 202V235H196V204 M184 220L196 204L208 220" fill="none" stroke="#990000" stroke-width="3"/>
<rect x="388" y="218" width="504" height="34" rx="17" fill="#F4F6FA"/>
<text x="640" y="241" text-anchor="middle" font-size="17" fill="#990000">Download candidate &#8594; translate &#8594; apply script</text>
<text x="640" y="283" text-anchor="middle" font-size="14" fill="#52637B">Manual boundaries: Fusion export / script application and Quilter web upload / download.</text>
</g>'''
    write("workflow.svg", workflow, "Conversion workflow",
          "Forward: Fusion to KiCad to Quilter. Return: download, translate, apply to the original Fusion PCB.", 310)


if __name__ == "__main__":
    main()
