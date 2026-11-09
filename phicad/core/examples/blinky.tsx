// A minimal PhiCad circuit: a resistor and an LED on an 18 x 12 mm board.
//   phicad check examples/blinky.tsx     evaluate and report findings
//   phicad build examples/blinky.tsx     + verify with PhiCad's KiCad DRC engine
export default () => (
  <board width="18mm" height="12mm">
    <resistor name="R1" resistance="1k" footprint="0402" pcbX={-4} />
    <led name="D1" color="red" footprint="0603" pcbX={4} />
    <trace name="anode" from=".R1 > .pin2" to=".D1 > .anode" />
    <trace name="return" from=".D1 > .cathode" to=".R1 > .pin1" />
  </board>
)
