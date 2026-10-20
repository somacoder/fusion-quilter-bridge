<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE eagle SYSTEM "eagle.dtd">
<eagle version="9.6.2">
  <drawing>
    <settings>
      <setting alwaysvectorfont="no" />
    </settings>
    <grid distance="0.1" unitdist="mm" unit="mm" style="lines" multiple="1" display="no" altdistance="0.01" altunitdist="mm" altunit="mm" />
    <layers>
      <layer number="1" name="Top" color="4" fill="1" visible="yes" active="yes" />
      <layer number="16" name="Bottom" color="1" fill="1" visible="yes" active="yes" />
      <layer number="17" name="Pads" color="1" fill="1" visible="yes" active="yes" />
      <layer number="18" name="Vias" color="1" fill="1" visible="yes" active="yes" />
      <layer number="19" name="Unrouted" color="1" fill="1" visible="yes" active="yes" />
      <layer number="20" name="Dimension" color="1" fill="1" visible="yes" active="yes" />
      <layer number="21" name="tPlace" color="1" fill="1" visible="yes" active="yes" />
      <layer number="22" name="bPlace" color="1" fill="1" visible="yes" active="yes" />
      <layer number="23" name="tOrigins" color="1" fill="1" visible="yes" active="yes" />
      <layer number="24" name="bOrigins" color="1" fill="1" visible="yes" active="yes" />
      <layer number="25" name="tNames" color="1" fill="1" visible="yes" active="yes" />
      <layer number="26" name="bNames" color="1" fill="1" visible="yes" active="yes" />
      <layer number="27" name="tValues" color="1" fill="1" visible="yes" active="yes" />
      <layer number="28" name="bValues" color="1" fill="1" visible="yes" active="yes" />
      <layer number="29" name="tStop" color="1" fill="1" visible="yes" active="yes" />
      <layer number="30" name="bStop" color="1" fill="1" visible="yes" active="yes" />
      <layer number="31" name="tCream" color="1" fill="1" visible="yes" active="yes" />
      <layer number="32" name="bCream" color="1" fill="1" visible="yes" active="yes" />
      <layer number="39" name="tKeepout" color="1" fill="1" visible="yes" active="yes" />
      <layer number="40" name="bKeepout" color="1" fill="1" visible="yes" active="yes" />
      <layer number="41" name="tRestrict" color="1" fill="1" visible="yes" active="yes" />
      <layer number="42" name="bRestrict" color="1" fill="1" visible="yes" active="yes" />
      <layer number="43" name="vRestrict" color="1" fill="1" visible="yes" active="yes" />
      <layer number="44" name="Drills" color="1" fill="1" visible="yes" active="yes" />
      <layer number="45" name="Holes" color="1" fill="1" visible="yes" active="yes" />
      <layer number="46" name="Milling" color="1" fill="1" visible="yes" active="yes" />
      <layer number="47" name="Measures" color="1" fill="1" visible="yes" active="yes" />
      <layer number="48" name="Document" color="1" fill="1" visible="yes" active="yes" />
      <layer number="51" name="tDocu" color="1" fill="1" visible="yes" active="yes" />
      <layer number="52" name="bDocu" color="1" fill="1" visible="yes" active="yes" />
      <layer number="91" name="Nets" color="1" fill="1" visible="yes" active="yes" />
      <layer number="92" name="Busses" color="1" fill="1" visible="yes" active="yes" />
      <layer number="93" name="Pins" color="1" fill="1" visible="yes" active="yes" />
      <layer number="94" name="Symbols" color="1" fill="1" visible="yes" active="yes" />
      <layer number="95" name="Names" color="1" fill="1" visible="yes" active="yes" />
      <layer number="96" name="Values" color="1" fill="1" visible="yes" active="yes" />
    </layers>
    <schematic>
      <plain />
      <libraries>
        <library name="bridge-demo">
          <description>Original synthetic test geometry; not an electronic product.</description>
          <packages>
            <package name="DEMO_SMD">
              <smd name="1" x="-2.54" y="0" dx="1.5" dy="2" layer="1" />
              <smd name="2" x="2.54" y="0" dx="1.5" dy="2" layer="1" />
              <wire x1="-1" y1="-1" x2="1" y2="-1" width="0.15" layer="21" />
              <wire x1="1" y1="-1" x2="1" y2="1" width="0.15" layer="21" />
              <wire x1="1" y1="1" x2="-1" y2="1" width="0.15" layer="21" />
              <wire x1="-1" y1="1" x2="-1" y2="-1" width="0.15" layer="21" />
              <text x="-1" y="2" size="1" layer="25">&gt;NAME</text>
              <text x="-1" y="-3" size="1" layer="27">&gt;VALUE</text>
              <wire x1="-1" y1="3.5" x2="2" y2="4" width="0.4" layer="41" />
            </package>
            <package name="DEMO_THT">
              <pad name="1" x="-2.54" y="0" drill="0.8" diameter="1.8" shape="round" />
              <pad name="2" x="2.54" y="0" drill="0.8" diameter="1.8" shape="round" />
              <wire x1="-1" y1="-1" x2="1" y2="-1" width="0.15" layer="21" />
              <wire x1="1" y1="-1" x2="1" y2="1" width="0.15" layer="21" />
              <wire x1="1" y1="1" x2="-1" y2="1" width="0.15" layer="21" />
              <wire x1="-1" y1="1" x2="-1" y2="-1" width="0.15" layer="21" />
              <text x="-1" y="2" size="1" layer="25">&gt;NAME</text>
              <text x="-1" y="-3" size="1" layer="27">&gt;VALUE</text>
            </package>
          </packages>
          <symbols>
            <symbol name="PASSIVE">
              <wire x1="-2.54" y1="-1.27" x2="2.54" y2="-1.27" width="0.254" layer="94" />
              <wire x1="2.54" y1="-1.27" x2="2.54" y2="1.27" width="0.254" layer="94" />
              <wire x1="2.54" y1="1.27" x2="-2.54" y2="1.27" width="0.254" layer="94" />
              <wire x1="-2.54" y1="1.27" x2="-2.54" y2="-1.27" width="0.254" layer="94" />
              <pin name="A" x="-5.08" y="0" visible="off" length="short" direction="pas" />
              <pin name="B" x="5.08" y="0" visible="off" length="short" direction="pas" rot="R180" />
              <text x="-2.54" y="2.54" size="1.27" layer="95">&gt;NAME</text>
              <text x="-2.54" y="-3.81" size="1.27" layer="96">&gt;VALUE</text>
            </symbol>
          </symbols>
          <devicesets>
            <deviceset name="DEMO_SMD" prefix="R" uservalue="yes">
              <gates>
                <gate name="G$1" symbol="PASSIVE" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="DEMO_SMD">
                  <connects>
                    <connect gate="G$1" pin="A" pad="1" />
                    <connect gate="G$1" pin="B" pad="2" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
            <deviceset name="DEMO_THT" prefix="R" uservalue="yes">
              <gates>
                <gate name="G$1" symbol="PASSIVE" x="0" y="0" />
              </gates>
              <devices>
                <device name="" package="DEMO_THT">
                  <connects>
                    <connect gate="G$1" pin="A" pad="1" />
                    <connect gate="G$1" pin="B" pad="2" />
                  </connects>
                  <technologies>
                    <technology name="" />
                  </technologies>
                </device>
              </devices>
            </deviceset>
          </devicesets>
        </library>
      </libraries>
      <attributes />
      <variantdefs />
      <classes>
        <class number="0" name="default" width="0" drill="0" />
      </classes>
      <parts>
        <part name="R1" library="bridge-demo" deviceset="DEMO_THT" device="" value="test-01" />
        <part name="R2" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-02" />
        <part name="R3" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-03" />
        <part name="R4" library="bridge-demo" deviceset="DEMO_THT" device="" value="test-04" />
        <part name="R5" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-05" />
        <part name="R6" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-06" />
        <part name="R7" library="bridge-demo" deviceset="DEMO_THT" device="" value="test-07" />
        <part name="R8" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-08" />
        <part name="R9" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-09" />
        <part name="R10" library="bridge-demo" deviceset="DEMO_THT" device="" value="test-10" />
        <part name="R11" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-11" />
        <part name="R12" library="bridge-demo" deviceset="DEMO_SMD" device="" value="test-12" />
      </parts>
      <sheets>
        <sheet>
          <plain />
          <instances>
            <instance part="R1" gate="G$1" x="25.4" y="101.6" smashed="no" />
            <instance part="R2" gate="G$1" x="55.879999999999995" y="101.6" smashed="no" />
            <instance part="R3" gate="G$1" x="86.36" y="101.6" smashed="no" />
            <instance part="R4" gate="G$1" x="116.84" y="101.6" smashed="no" />
            <instance part="R5" gate="G$1" x="25.4" y="76.19999999999999" smashed="no" />
            <instance part="R6" gate="G$1" x="55.879999999999995" y="76.19999999999999" smashed="no" />
            <instance part="R7" gate="G$1" x="86.36" y="76.19999999999999" smashed="no" />
            <instance part="R8" gate="G$1" x="116.84" y="76.19999999999999" smashed="no" />
            <instance part="R9" gate="G$1" x="25.4" y="50.8" smashed="no" />
            <instance part="R10" gate="G$1" x="55.879999999999995" y="50.8" smashed="no" />
            <instance part="R11" gate="G$1" x="86.36" y="50.8" smashed="no" />
            <instance part="R12" gate="G$1" x="116.84" y="50.8" smashed="no" />
          </instances>
          <busses />
          <nets>
            <net name="NET_A" class="0">
              <segment>
                <pinref part="R1" gate="G$1" pin="A" />
                <wire x1="20.32" y1="101.6" x2="15.24" y2="101.6" width="0.1524" layer="91" />
                <label x="15.24" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R4" gate="G$1" pin="B" />
                <wire x1="121.92" y1="101.6" x2="127.0" y2="101.6" width="0.1524" layer="91" />
                <label x="127.0" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R5" gate="G$1" pin="A" />
                <wire x1="20.32" y1="76.19999999999999" x2="15.24" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="15.24" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R8" gate="G$1" pin="B" />
                <wire x1="121.92" y1="76.19999999999999" x2="127.0" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="127.0" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R9" gate="G$1" pin="A" />
                <wire x1="20.32" y1="50.8" x2="15.24" y2="50.8" width="0.1524" layer="91" />
                <label x="15.24" y="50.8" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R12" gate="G$1" pin="B" />
                <wire x1="121.92" y1="50.8" x2="127.0" y2="50.8" width="0.1524" layer="91" />
                <label x="127.0" y="50.8" size="1.27" layer="95" />
              </segment>
            </net>
            <net name="NET_B" class="0">
              <segment>
                <pinref part="R1" gate="G$1" pin="B" />
                <wire x1="30.479999999999997" y1="101.6" x2="35.559999999999995" y2="101.6" width="0.1524" layer="91" />
                <label x="35.559999999999995" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R2" gate="G$1" pin="A" />
                <wire x1="50.8" y1="101.6" x2="45.72" y2="101.6" width="0.1524" layer="91" />
                <label x="45.72" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R5" gate="G$1" pin="B" />
                <wire x1="30.479999999999997" y1="76.19999999999999" x2="35.559999999999995" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="35.559999999999995" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R6" gate="G$1" pin="A" />
                <wire x1="50.8" y1="76.19999999999999" x2="45.72" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="45.72" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R9" gate="G$1" pin="B" />
                <wire x1="30.479999999999997" y1="50.8" x2="35.559999999999995" y2="50.8" width="0.1524" layer="91" />
                <label x="35.559999999999995" y="50.8" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R10" gate="G$1" pin="A" />
                <wire x1="50.8" y1="50.8" x2="45.72" y2="50.8" width="0.1524" layer="91" />
                <label x="45.72" y="50.8" size="1.27" layer="95" />
              </segment>
            </net>
            <net name="NET_C" class="0">
              <segment>
                <pinref part="R2" gate="G$1" pin="B" />
                <wire x1="60.959999999999994" y1="101.6" x2="66.03999999999999" y2="101.6" width="0.1524" layer="91" />
                <label x="66.03999999999999" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R3" gate="G$1" pin="A" />
                <wire x1="81.28" y1="101.6" x2="76.2" y2="101.6" width="0.1524" layer="91" />
                <label x="76.2" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R6" gate="G$1" pin="B" />
                <wire x1="60.959999999999994" y1="76.19999999999999" x2="66.03999999999999" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="66.03999999999999" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R7" gate="G$1" pin="A" />
                <wire x1="81.28" y1="76.19999999999999" x2="76.2" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="76.2" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R10" gate="G$1" pin="B" />
                <wire x1="60.959999999999994" y1="50.8" x2="66.03999999999999" y2="50.8" width="0.1524" layer="91" />
                <label x="66.03999999999999" y="50.8" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R11" gate="G$1" pin="A" />
                <wire x1="81.28" y1="50.8" x2="76.2" y2="50.8" width="0.1524" layer="91" />
                <label x="76.2" y="50.8" size="1.27" layer="95" />
              </segment>
            </net>
            <net name="NET_D" class="0">
              <segment>
                <pinref part="R3" gate="G$1" pin="B" />
                <wire x1="91.44" y1="101.6" x2="96.52" y2="101.6" width="0.1524" layer="91" />
                <label x="96.52" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R4" gate="G$1" pin="A" />
                <wire x1="111.76" y1="101.6" x2="106.68" y2="101.6" width="0.1524" layer="91" />
                <label x="106.68" y="101.6" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R7" gate="G$1" pin="B" />
                <wire x1="91.44" y1="76.19999999999999" x2="96.52" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="96.52" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R8" gate="G$1" pin="A" />
                <wire x1="111.76" y1="76.19999999999999" x2="106.68" y2="76.19999999999999" width="0.1524" layer="91" />
                <label x="106.68" y="76.19999999999999" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R11" gate="G$1" pin="B" />
                <wire x1="91.44" y1="50.8" x2="96.52" y2="50.8" width="0.1524" layer="91" />
                <label x="96.52" y="50.8" size="1.27" layer="95" />
              </segment>
              <segment>
                <pinref part="R12" gate="G$1" pin="A" />
                <wire x1="111.76" y1="50.8" x2="106.68" y2="50.8" width="0.1524" layer="91" />
                <label x="106.68" y="50.8" size="1.27" layer="95" />
              </segment>
            </net>
          </nets>
        </sheet>
      </sheets>
    </schematic>
  </drawing>
</eagle>
