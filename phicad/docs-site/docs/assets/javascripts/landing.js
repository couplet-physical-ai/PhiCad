/*
 * PhiCad product landing interactions.
 * Copyright (C) 2026 Exowatt, Inc.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * Motion explains hierarchy and makes the native board feel like the page's working object.
 * Everything remains readable without this file, and reduced-motion mode skips transforms.
 */

(function phicadLandingModule(root) {
  "use strict";

  let activeController = null;
  let activeObserver = null;

  function showAll(landing) {
    landing.querySelectorAll(".landing-reveal").forEach(function show(element) {
      element.classList.add("is-visible");
    });
  }

  function initialize() {
    const landing = root.document.querySelector("[data-phicad-landing]");
    if (!landing) {
      if (activeController) activeController.abort();
      if (activeObserver) activeObserver.disconnect();
      activeController = null;
      activeObserver = null;
      return;
    }

    if (landing.dataset.landingReady === "true") return;
    landing.dataset.landingReady = "true";

    if (activeController) activeController.abort();
    if (activeObserver) activeObserver.disconnect();
    activeController = typeof AbortController === "function" ? new AbortController() : null;
    const eventOptions = activeController ? { signal: activeController.signal } : undefined;
    const reducedMotion = root.matchMedia("(prefers-reduced-motion: reduce)").matches;

    landing.classList.add("is-enhanced");

    const nav = landing.querySelector(".product-nav");
    if (nav) {
      const navLinks = Array.from(nav.querySelectorAll(".product-nav-links a"));
      const sections = navLinks.map(function resolveSection(link) {
        return { link: link, section: root.document.getElementById(link.hash.slice(1)) };
      }).filter(function keepResolved(item) {
        return item.section;
      });
      let navFrame = 0;

      const updateNav = function updateNav() {
        navFrame = 0;
        const scrollTop = root.scrollY || root.document.documentElement.scrollTop || 0;
        const scrollRange = Math.max(1, root.document.documentElement.scrollHeight - root.innerHeight);
        const progress = Math.max(0, Math.min(1, scrollTop / scrollRange));
        const activeOffset = Math.min(240, Math.max(nav.offsetHeight + 96, root.innerHeight * 0.18));
        const readingLine = scrollTop + activeOffset;
        let current = null;

        sections.forEach(function findCurrent(item) {
          if (item.section.offsetTop <= readingLine) current = item;
        });
        sections.forEach(function markCurrent(item) {
          const active = item === current;
          item.link.classList.toggle("is-active", active);
          if (active) item.link.setAttribute("aria-current", "location");
          else item.link.removeAttribute("aria-current");
        });

        nav.classList.toggle("is-scrolled", scrollTop > 12);
        nav.style.setProperty("--landing-scroll-progress", progress.toFixed(4));
      };
      const queueNavUpdate = function queueNavUpdate() {
        if (navFrame) return;
        navFrame = root.requestAnimationFrame(updateNav);
      };

      updateNav();
      root.addEventListener("scroll", queueNavUpdate, Object.assign({ passive: true }, eventOptions || {}));
      root.addEventListener("resize", queueNavUpdate, Object.assign({ passive: true }, eventOptions || {}));
    }

    landing.querySelectorAll(".output-gallery, .route-grid").forEach(function stagger(group) {
      Array.from(group.children).forEach(function setIndex(child, index) {
        child.style.setProperty("--landing-index", String(index));
      });
    });

    if (reducedMotion || !("IntersectionObserver" in root)) {
      showAll(landing);
    } else {
      try {
        activeObserver = new IntersectionObserver(function reveal(entries) {
          entries.forEach(function revealEntry(entry) {
            if (!entry.isIntersecting) return;
            entry.target.classList.add("is-visible");
            activeObserver.unobserve(entry.target);
          });
        }, { rootMargin: "0px 0px -9% 0px", threshold: 0.08 });
        landing.querySelectorAll(".landing-reveal").forEach(function observe(element) {
          activeObserver.observe(element);
        });
      } catch (error) {
        showAll(landing);
      }
    }

    const hero = landing.querySelector(".landing-hero");
    const board = landing.querySelector(".hero-board-frame");
    if (hero && board && !reducedMotion && root.matchMedia("(pointer: fine)").matches) {
      const resetBoard = function resetBoard() {
        board.style.removeProperty("--landing-board-x");
        board.style.removeProperty("--landing-board-z");
        board.style.removeProperty("--landing-board-y");
      };
      hero.addEventListener("pointermove", function moveBoard(event) {
        const bounds = hero.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
        const y = Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height));
        board.style.setProperty("--landing-board-x", (54 + y * 6).toFixed(2) + "deg");
        board.style.setProperty("--landing-board-z", (-15 + x * 6).toFixed(2) + "deg");
        board.style.setProperty("--landing-board-y", ((y - 0.5) * 18).toFixed(2) + "px");
      }, eventOptions);
      hero.addEventListener("pointerleave", resetBoard, eventOptions);
    }

    root.__phicadLanding = {
      ready: true,
      currentProductImages: landing.querySelectorAll("img").length,
      reducedMotion: reducedMotion
    };
  }

  if (root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", initialize, { once: true });
  } else {
    initialize();
  }

  if (root.document$ && typeof root.document$.subscribe === "function") {
    root.document$.subscribe(initialize);
  }
})(window);
