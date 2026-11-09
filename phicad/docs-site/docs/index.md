<div class="phicad-landing" id="top" data-phicad-landing>
  <a class="landing-skip" href="#landing-content">Skip to PhiCad content</a>

  <nav class="product-nav" aria-label="PhiCad product navigation">
    <div class="landing-wrap product-nav-inner">
      <a class="product-brand" href="." aria-label="PhiCad home">
        <span class="product-brand-rule" aria-hidden="true"></span>
        <strong>PhiCad</strong>
      </a>
      <div class="product-nav-links" role="region" aria-label="PhiCad page sections" tabindex="0">
        <a href="#overview">Overview</a>
        <a href="#output">Output</a>
        <a href="#how">How it works</a>
        <a href="#direction">Direction</a>
      </div>
      <a class="product-nav-cta" href="guides/getting-started/">Get started</a>
    </div>
    <span class="product-nav-progress" aria-hidden="true"></span>
  </nav>

  <header class="landing-hero" id="landing-content">
    <svg class="hero-traces" viewBox="0 0 1600 900" preserveAspectRatio="none" aria-hidden="true">
      <path d="M0 700 H225 L345 580 H630 L760 450 H1030 L1185 295 H1600"></path>
      <path d="M0 785 H300 L425 660 H725 L830 555 H1110 L1280 385 H1600"></path>
      <circle cx="345" cy="580" r="7"></circle>
      <circle cx="760" cy="450" r="7"></circle>
      <circle cx="1185" cy="295" r="7"></circle>
    </svg>

    <div class="hero-board-stage" aria-hidden="true">
      <div class="hero-board-halo"></div>
      <figure class="hero-board-frame">
        <img src="assets/product/3d-view.png" alt="" width="1376" height="984" fetchpriority="high">
      </figure>
    </div>

    <div class="landing-wrap hero-wrap">
      <div class="hero-copy">
        <p class="landing-eyebrow landing-reveal">Electrical design, generated</p>
        <h1 class="landing-reveal">
          <span class="hero-dim">Describe<br>the circuit.</span><br>
          <span class="hero-gradient">Inspect the board.</span>
        </h1>
        <p class="hero-lede landing-reveal">PhiCad turns circuit intent into a native KiCad project—with the schematic, PCB, 3D view, and named findings close enough to review.</p>
        <div class="hero-actions landing-reveal" aria-label="Primary PhiCad links">
          <a class="button button-primary" href="guides/getting-started/">Start with a real build</a>
          <a class="button button-ghost" href="#output">See the output</a>
        </div>
        <p class="hero-note landing-reveal"><span aria-hidden="true"></span> Real output from the current checkout. No concept UI in this hero.</p>
      </div>
    </div>

    <a class="scroll-cue" href="#overview" aria-label="Scroll to the PhiCad product overview">
      <span class="scroll-cue-label">Scroll to explore</span>
      <span class="scroll-cue-track" aria-hidden="true"><span></span></span>
    </a>
  </header>

  <section class="signal-band" id="overview" aria-label="Current PhiCad surface">
    <div class="landing-wrap signal-grid">
      <div class="signal landing-reveal"><b>1</b><span>Editable KiCad project from one design intent</span></div>
      <div class="signal landing-reveal"><b>3</b><span>Native views: schematic, PCB, and 3D</span></div>
      <div class="signal landing-reveal"><b>123</b><span>Documented MCP tools for agents and automation</span></div>
      <div class="signal landing-reveal"><b>16</b><span>Tool sections across the design workflow</span></div>
    </div>
  </section>

  <div class="landing-main">
    <section class="landing-section output-section" id="output" aria-labelledby="output-heading">
      <div class="landing-wrap">
        <div class="section-heading">
          <div>
            <p class="landing-eyebrow landing-accent landing-reveal">One project</p>
            <h2 id="output-heading" class="landing-reveal">Every view stays<br>close to the work.</h2>
          </div>
          <p class="section-lede landing-reveal">The strongest proof is the project itself. These are current PhiCad outputs—not illustrations of what the product might do later.</p>
        </div>

        <div class="output-gallery">
          <figure class="output-card landing-reveal">
            <div class="output-image output-image-schematic">
              <img src="assets/product/schematic-wires.png" alt="Native PhiCad schematic showing components, labels, and connected nets" width="1200" height="849" loading="lazy" decoding="async">
            </div>
            <figcaption><b>Schematic</b><span>Native .kicad_sch · open and edit</span></figcaption>
          </figure>
          <figure class="output-card landing-reveal">
            <div class="output-image output-image-board">
              <img src="assets/product/vis-p3-routed.png" alt="Routed sensor-node board rendered by PhiCad" width="700" height="1038" loading="lazy" decoding="async">
            </div>
            <figcaption><b>PCB</b><span>Native .kicad_pcb · routed geometry</span></figcaption>
          </figure>
          <figure class="output-card landing-reveal">
            <div class="output-image output-image-3d">
              <img src="assets/product/3d-view.png" alt="Native 3D view of a routed board with components and copper traces" width="1376" height="984" loading="lazy" decoding="async">
            </div>
            <figcaption><b>3D</b><span>Native renderer · physical review</span></figcaption>
          </figure>
          <figure class="output-card landing-reveal">
            <div class="output-image output-image-agent">
              <img src="assets/product/agent-view.png" alt="PhiCad Agent View showing a board beside its project-scoped conversation" width="1500" height="950" loading="lazy" decoding="async">
            </div>
            <figcaption><b>Agent View</b><span>Board + project context · one surface</span></figcaption>
          </figure>
        </div>
      </div>
    </section>

    <section class="landing-section run-section" id="evidence" aria-labelledby="run-heading">
      <div class="landing-wrap">
        <div class="run-intro">
          <p class="landing-eyebrow landing-accent landing-reveal">See the current build</p>
          <h2 id="run-heading" class="landing-reveal">Watch the board<br>take shape.</h2>
          <p class="section-lede landing-reveal">A real synthesis run moves through validation, placement, schematic generation, routing, fill, and native DRC evidence. The recording ends where the measured verdict ends.</p>
        </div>

        <figure class="run-feature landing-reveal">
          <div class="run-video">
            <p class="run-badge">Current build · intent → project</p>
            <video controls muted playsinline preload="metadata" poster="assets/getting-started/phase3-routed.png" aria-describedby="pipeline-video-caption">
              <source src="assets/getting-started/pipeline-demo.mp4" type="video/mp4">
              Your browser cannot play this video. <a href="assets/getting-started/pipeline-demo.mp4">Download the pipeline recording</a> or <a href="#pipeline-video-transcript">read the transcript</a>.
            </video>
          </div>
          <figcaption id="pipeline-video-caption">
            <div>
              <p class="landing-eyebrow landing-accent">A run you can repeat</p>
              <h3>From description to native files.</h3>
            </div>
            <div>
              <p>The build writes an ordinary KiCad project and reports each gate independently. A failure stays visible instead of being softened into a success state.</p>
              <details id="pipeline-video-transcript">
                <summary>Read the transcript</summary>
                <p>The terminal starts a PhiCad synthesis build, reports each stage independently, writes the KiCad project, and ends on the measured gate verdict before the routed board is inspected.</p>
              </details>
            </div>
          </figcaption>
        </figure>
      </div>
    </section>

    <section class="landing-section process-section" id="how" aria-labelledby="how-heading">
      <div class="landing-wrap process-grid">
        <div class="process-copy">
          <p class="landing-eyebrow landing-accent landing-reveal">How it works</p>
          <h2 id="how-heading" class="landing-reveal">Intent in.<br>Evidence out.</h2>
          <p class="section-lede landing-reveal">The workflow keeps the generated geometry and the reason to trust—or reject—it in the same project.</p>
          <a class="text-link landing-reveal" href="reference/synth/">Read about board synthesis <span aria-hidden="true">→</span></a>
        </div>

        <ol class="process-steps">
          <li class="landing-reveal">
            <span class="process-number">01</span>
            <div><h3>Describe</h3><p>Record the circuit intent, constraints, parts, and connections in a reviewable design file.</p></div>
          </li>
          <li class="landing-reveal">
            <span class="process-number">02</span>
            <div><h3>Compile</h3><p>Generate native schematic and PCB files rather than a screenshot or a proprietary dead end.</p></div>
          </li>
          <li class="landing-reveal">
            <span class="process-number">03</span>
            <div><h3>Route</h3><p>Turn placement and connectivity into geometry that can be opened and changed in PhiCad or stock KiCad.</p></div>
          </li>
          <li class="landing-reveal">
            <span class="process-number">04</span>
            <div><h3>Prove</h3><p>Read each check independently. A clean DRC is evidence—not an automatic fabrication guarantee.</p></div>
          </li>
        </ol>
      </div>
    </section>

    <section class="landing-section proof-section" aria-labelledby="proof-heading">
      <div class="landing-wrap proof-grid">
        <div class="proof-copy">
          <p class="landing-eyebrow landing-accent landing-reveal">The handoff</p>
          <h2 id="proof-heading" class="landing-reveal">A project,<br>not a picture.</h2>
          <p class="section-lede landing-reveal">Open the output in PhiCad—or in stock KiCad—and continue by hand. Generated boards remain drafts to review, with the findings and limitations left attached.</p>
          <div class="proof-actions landing-reveal">
            <a class="button button-primary" href="guides/getting-started/">Follow a real run</a>
            <a class="text-link" href="reference/testing/">See the testing approach <span aria-hidden="true">→</span></a>
          </div>
        </div>
        <div class="proof-points" aria-label="What the handoff preserves">
          <div class="proof-point landing-reveal"><span>01</span><div><h3>Editable</h3><p>Native files stay in the familiar KiCad workflow.</p></div></div>
          <div class="proof-point landing-reveal"><span>02</span><div><h3>Local project</h3><p>Board and schematic files stay on your machine by default; AI requests use your authenticated Phi identity.</p></div></div>
          <div class="proof-point landing-reveal"><span>03</span><div><h3>Traceable</h3><p>Findings carry evidence about the item, layer, and location involved.</p></div></div>
        </div>
      </div>
    </section>

    <section class="landing-section direction-section" id="direction" aria-labelledby="direction-heading">
      <div class="landing-wrap">
        <div class="direction-intro">
          <div>
            <p class="landing-eyebrow landing-accent landing-reveal">What comes next</p>
            <h2 id="direction-heading" class="landing-reveal">A broader surface<br>is taking shape.</h2>
          </div>
          <p class="section-lede landing-reveal">The Studio concept explores a more connected way to move through schematic, PCB, 3D, BOM, and DRC views. It is product direction—not the authority for a fabrication handoff.</p>
        </div>
      </div>
    </section>

    <section class="landing-section start-section" aria-labelledby="start-heading">
      <div class="landing-wrap">
        <div class="section-heading start-heading">
          <div>
            <p class="landing-eyebrow landing-accent landing-reveal">Start here</p>
            <h2 id="start-heading" class="landing-reveal">Choose your<br>next move.</h2>
          </div>
          <p class="section-lede landing-reveal">One project, several ways in. Start with the <a href="guides/product-tour/">visual product tour</a> or choose the route that matches your work.</p>
        </div>

        <nav class="route-grid" aria-label="PhiCad documentation routes">
          <a class="route-card route-card-featured landing-reveal" href="guides/getting-started/">
            <span class="route-label">First run</span><h3>Getting started</h3><p>Build a design and learn how to read its gates.</p><span class="route-arrow" aria-hidden="true">↗</span>
          </a>
          <a class="route-card landing-reveal" href="guides/the-application/">
            <span class="route-label">In the editor</span><h3>The application</h3><p>Find the GUI, Agent View, and native editing loop.</p><span class="route-arrow" aria-hidden="true">↗</span>
          </a>
          <a class="route-card landing-reveal" href="guides/driving-phicad/">
            <span class="route-label">From a shell</span><h3>Drive PhiCad</h3><p>Use the CLI, an agent, or an MCP client.</p><span class="route-arrow" aria-hidden="true">↗</span>
          </a>
          <a class="route-card landing-reveal" href="reference/architecture/">
            <span class="route-label">Under the hood</span><h3>Reference</h3><p>Understand the interfaces, architecture, and boundaries.</p><span class="route-arrow" aria-hidden="true">↗</span>
          </a>
        </nav>
      </div>
    </section>

    <section class="landing-cta" aria-labelledby="cta-heading">
      <div class="landing-wrap cta-grid">
        <div>
          <p class="landing-eyebrow landing-reveal">Try it on a real circuit</p>
          <h2 id="cta-heading" class="landing-reveal">Bring the design<br>you need to inspect.</h2>
        </div>
        <div class="cta-copy landing-reveal">
          <p>Start with a small board, keep every generated file, and decide from the evidence whether it is ready for the next step.</p>
          <div class="hero-actions">
            <a class="button button-invert" href="guides/getting-started/">Build the example</a>
            <a class="button button-outline" href="guides/product-tour/">Take the product tour</a>
          </div>
        </div>
      </div>
    </section>
  </div>

  <footer class="landing-footer">
    <div class="landing-wrap footer-wrap">
      <div class="footer-brand">
        <p>PhiCad is a fork of KiCad 10, with a local, evidence-led design loop.</p>
      </div>
      <nav class="footer-links" aria-label="PhiCad footer navigation">
        <a href="guides/getting-started/">Get started</a>
        <a href="reference/mcp-tools/">MCP tools</a>
        <a href="guides/feedback/">Feedback</a>
        <a href="reference/look-and-feel/">Look and feel</a>
        <a href="#top">Back to top ↑</a>
      </nav>
    </div>
  </footer>
</div>
