/*
 * Shared reference-manual "chrome" behaviour: TOC accordion, live search,
 * and scroll-spy active-section highlighting. Pairs with manual-chrome.css.
 *
 * Markup contract (see manual-chrome.css's header comment for the full
 * adoption steps):
 *   #chrome-search              text input, search-as-you-type
 *   #chrome-toc                 <ul>, one of:
 *     li.chrome-toc-group         a collapsible group:
 *       > button.chrome-toc-group-btn   the group's clickable label
 *       > ul > li > a[href="#id"]       its entries
 *     li > a[href="#id"]            a standalone (ungrouped) entry
 *   .chrome-content section[id] each section jumped to by a TOC entry
 *
 * No build step, no dependencies -- vanilla JS, runs after DOMContentLoaded.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    initAccordion();
    initSearch();
    initScrollSpy();
  });

  function initAccordion() {
    var groups = document.querySelectorAll(".chrome-toc-group");
    groups.forEach(function (group) {
      var btn = group.querySelector(".chrome-toc-group-btn");
      if (!btn) return;
      btn.addEventListener("click", function () {
        group.classList.toggle("collapsed");
      });
    });
  }

  function initSearch() {
    var input = document.getElementById("chrome-search");
    var toc = document.getElementById("chrome-toc");
    if (!input || !toc) return;

    var entries = Array.prototype.slice.call(toc.querySelectorAll("a[href^='#']"));
    var noResults = toc.querySelector(".chrome-no-results");

    // Cache each TOC entry's target section text once, up front -- this is a
    // manual's whole point: a search for "crosshair" must find the section
    // that MENTIONS crosshairs even though no heading is titled that.
    var sectionTextByHref = {};
    entries.forEach(function (a) {
      var href = a.getAttribute("href");
      var target = href && document.querySelector(href);
      sectionTextByHref[href] = target ? target.textContent.toLowerCase() : "";
    });

    input.addEventListener("input", function () {
      var query = input.value.trim().toLowerCase();
      var anyVisible = false;

      entries.forEach(function (a) {
        var li = a.closest("li");
        var href = a.getAttribute("href");
        var match =
          !query ||
          a.textContent.toLowerCase().indexOf(query) !== -1 ||
          (sectionTextByHref[href] || "").indexOf(query) !== -1;
        if (li) li.classList.toggle("chrome-hidden", !match);
        if (match) anyVisible = true;
      });

      // A group stays visible if the query is empty, or at least one of its
      // own entries matched -- otherwise collapse it out of the way.
      document.querySelectorAll(".chrome-toc-group").forEach(function (group) {
        var groupLi = group; // the group IS the <li>
        if (!query) {
          groupLi.classList.remove("chrome-hidden");
          return;
        }
        var hasVisibleChild = group.querySelectorAll("li:not(.chrome-hidden)").length > 0;
        var btnText = group.querySelector(".chrome-toc-group-btn");
        var groupNameMatches = btnText && btnText.textContent.toLowerCase().indexOf(query) !== -1;
        groupLi.classList.toggle("chrome-hidden", !hasVisibleChild && !groupNameMatches);
        if (groupNameMatches) {
          group.querySelectorAll("li").forEach(function (li) {
            li.classList.remove("chrome-hidden");
          });
        }
      });

      if (noResults) {
        noResults.style.display = anyVisible || !query ? "none" : "block";
      }
    });
  }

  function initScrollSpy() {
    var sections = Array.prototype.slice.call(
      document.querySelectorAll(".chrome-content section[id]")
    );
    var links = Array.prototype.slice.call(
      document.querySelectorAll('#chrome-toc a[href^="#"]')
    );
    if (!sections.length || !links.length || !("IntersectionObserver" in window)) return;

    var linkByHref = {};
    links.forEach(function (a) {
      linkByHref[a.getAttribute("href")] = a;
    });

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          var link = linkByHref["#" + entry.target.id];
          if (!link) return;
          if (entry.isIntersecting) {
            links.forEach(function (a) {
              a.classList.remove("chrome-active");
            });
            link.classList.add("chrome-active");
            // Keep the active TOC entry's group expanded so it's visible.
            var group = link.closest(".chrome-toc-group");
            if (group) group.classList.remove("collapsed");
          }
        });
      },
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 }
    );

    sections.forEach(function (section) {
      observer.observe(section);
    });
  }
})();
