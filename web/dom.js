/* Small DOM helpers: element building, toasts, and modal dialogs. */

/** el('div.foo', {onclick}, ...children) — tag may carry .classes. */
export function el(spec, attrs, ...children) {
  const [tag, ...classes] = String(spec).split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = [node.className, v].filter(Boolean).join(' ');
      else if (k === 'style') node.style.cssText = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(node.dataset, v);
      else node.setAttribute(k, v === true ? '' : v);
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export const $ = (id) => document.getElementById(id);

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function fill(node, ...children) {
  append(clear(node), children);
  return node;
}

export function show(node, visible) {
  node.classList.toggle('hidden', !visible);
}

/* --- toasts -------------------------------------------------------------- */

export function toast(message, kind = 'info', ttl = 5200) {
  const box = $('toasts');
  const kinds = { error: 'PROBLEM', ok: 'DONE', info: 'NOTE' };
  const node = el(`div.toast.${kind}`, {}, el('span.k', { text: kinds[kind] || 'NOTE' }), message);
  box.append(node);
  setTimeout(() => node.remove(), ttl);
  return node;
}

/* --- modals -------------------------------------------------------------- */

/**
 * Open a dialog. `fields` describe inputs; resolves with a values object keyed
 * by field name, or null if the user cancelled.
 *
 * field: { name, label, value, type: 'text'|'password'|'select'|'toggle',
 *          options: [...], mono: bool, placeholder, hint }
 */
export function dialog({ title, description, fields = [], confirmText = 'OK', danger = false }) {
  return new Promise((resolve) => {
    const inputs = new Map();

    const body = el('div.modal-body');
    for (const f of fields) {
      let input;
      if (f.type === 'select') {
        input = el('select.input', { id: `dlg-${f.name}` },
          f.options.map((o) => el('option', { value: o, selected: o === f.value }, o)));
      } else if (f.type === 'toggle') {
        input = el('div.toggle' + (f.value ? '.on' : ''), {
          onclick: () => input.classList.toggle('on'),
        }, el('div.knob'));
      } else {
        input = el('input.input' + (f.mono ? '.mono' : ''), {
          id: `dlg-${f.name}`, type: f.type || 'text', value: f.value ?? '',
          placeholder: f.placeholder || '', autocomplete: 'off', spellcheck: 'false',
        });
      }
      inputs.set(f.name, { field: f, input });

      if (f.type === 'toggle') {
        body.append(el('div.switch-row', {},
          el('div', {}, el('div.t', { text: f.label }), f.hint && el('div.d', { text: f.hint })),
          input));
      } else {
        body.append(el('div', {},
          el('label.field-label', { for: `dlg-${f.name}`, text: f.label }),
          input,
          f.hint && el('div', { style: 'font-size:11px;color:var(--dim-2);margin-top:6px', text: f.hint })));
      }
    }

    const finish = (value) => {
      document.removeEventListener('keydown', onKey, true);
      backdrop.remove();
      resolve(value);
    };

    const submit = () => {
      const out = {};
      for (const [name, { field, input }] of inputs) {
        out[name] = field.type === 'toggle' ? input.classList.contains('on') : input.value;
      }
      finish(out);
    };

    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.stopPropagation(); finish(null); }
      else if (ev.key === 'Enter' && ev.target.tagName !== 'TEXTAREA') { ev.stopPropagation(); submit(); }
    };

    const backdrop = el('div.backdrop', {
      onmousedown: (ev) => { if (ev.target === backdrop) finish(null); },
    }, el('div.modal', {},
      el('div.modal-head', {},
        el('div.modal-title', { text: title }),
        description && el('div.modal-desc', { text: description })),
      fields.length ? body : el('div', { style: 'height:16px' }),
      el('div.modal-foot', {},
        el('button.btn', { onclick: () => finish(null) }, 'Cancel'),
        el(`button.btn.${danger ? 'btn-danger' : 'btn-primary'}`, { onclick: submit }, confirmText))));

    document.addEventListener('keydown', onKey, true);
    document.body.append(backdrop);

    const first = [...inputs.values()].find((i) => i.field.type !== 'toggle');
    if (first) { first.input.focus(); first.input.select?.(); }
  });
}

/** Yes/no confirmation. Resolves true when confirmed. */
export async function confirmDialog({ title, description, confirmText = 'Confirm', danger = true }) {
  return (await dialog({ title, description, fields: [], confirmText, danger })) !== null;
}

/** Copy to clipboard with a graceful fallback for non-secure contexts. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = el('textarea', { style: 'position:fixed;opacity:0' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}
