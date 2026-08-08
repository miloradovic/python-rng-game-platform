"use strict";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("shell", () => ({
    navOpen: false,

    get navClass() {
      return this.navOpen ? "is-open" : "";
    },

    markReady() {
      document.documentElement.dataset.alpineReady = "true";
    },

    toggleNavigation() {
      this.navOpen = !this.navOpen;
    },
  }));
});
