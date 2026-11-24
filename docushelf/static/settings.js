// Settings area: small conveniences only, everything works without JavaScript.

// On phones the navigation is a row of tabs: show the current one.
const nav = document.querySelector(".settings-nav");
const current = nav && nav.querySelector(".is-active");
if (nav && current && nav.scrollWidth > nav.clientWidth) {
  nav.scrollLeft = current.offsetLeft - (nav.clientWidth - current.offsetWidth) / 2;
}

// A form that came back with an error is often further down the page: bring it into view.
const problem = document.querySelector(".alert-error[data-settings-message]");
if (problem) problem.scrollIntoView({ block: "center" });

// The language model: the address field suggests what the chosen provider usually needs.
const provider = document.querySelector('select[name="llm_provider"]');
const url = document.querySelector('input[name="llm_url"]');
const model = document.querySelector('input[name="llm_model"]');
if (provider && url) {
  const hints = {
    ollama: ["http://ollama:11434", "qwen2.5:7b"],
    openai: ["http://llama:8080/v1", "Modellname"],
    anthropic: ["https://api.anthropic.com", "Modellname"],
  };
  const update = () => {
    const [address, name] = hints[provider.value] || hints.ollama;
    url.placeholder = address;
    if (model) model.placeholder = name;
  };
  provider.addEventListener("change", update);
  update();
}
