/* Small DOM helpers: element building, toasts, and modal dialogs. */

/** el('div.foo', {onclick}, ...children); the tag may carry .classes. */
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

/**
 * Attributes that make a non-button element behave like one for the keyboard.
 * Rows and cards are divs because they nest their own buttons. Spread into an
 * el() attrs object.
 */
export function clickable(onActivate, label) {
  return {
    role: 'button',
    tabindex: '0',
    'aria-label': label || null,
    onclick: onActivate,
    onkeydown: (ev) => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      if (ev.target !== ev.currentTarget) return;   // let nested controls win
      ev.preventDefault();
      onActivate(ev);
    },
  };
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function fill(node, ...children) {
  append(clear(node), children);
  return node;
}

/**
 * Rebuild `node`'s children only when `sig` changes; `build()` returns them.
 * The app renders wholesale, so without this a scroll container would reset
 * mid-read and an open <select> would close. Returns true when it rebuilt.
 */
export function repaint(node, sig, build) {
  if (node.dataset.sig === sig) return false;
  node.dataset.sig = sig;
  fill(node, build());
  return true;
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
 * Open a dialog. Resolves with a values object keyed by field name, or null on
 * cancel.
 *
 * field: { name, label, value, type: 'text'|'password'|'select'|'toggle',
 *          options: [...], mono: bool, placeholder, hint }
 *
 * `onMount({ get, set, input })` runs once after the inputs exist, for wiring
 * one field to another. Programmatic `set` fires no input event, so cross-field
 * updates cannot loop.
 */
function optionNodes(options, selected) {
  // An option is a bare string (value doubles as label) or { value, label }.
  return (options || []).map((o) => {
    const value = o.value ?? o;
    return el('option', { value, selected: value === selected }, o.label ?? o);
  });
}

/**
 * `detail` is preformatted output shown verbatim (a server's last terminal
 * lines, say); `cancelText: null` drops Cancel, for a dialog that only reports.
 */
export function dialog({
  title, description, detail, fields = [], confirmText = 'OK', cancelText = 'Cancel',
  danger = false, onMount, validate,
}) {
  return new Promise((resolve) => {
    const inputs = new Map();

    const body = el('div.modal-body');
    for (const f of fields) {
      let input;
      if (f.type === 'select') {
        input = el('select.input', { id: `dlg-${f.name}` }, optionNodes(f.options, f.value));
      } else if (f.type === 'segmented') {
        // A pill selector: one button per option, the selected one pressed.
        input = el('div.segmented', { role: 'tablist', 'aria-label': f.label });
        input.dataset.value = f.value ?? (f.options[0].value ?? f.options[0]);
        for (const o of f.options) {
          const value = o.value ?? o;
          input.append(el('button.segment', {
            type: 'button', role: 'tab', dataset: { value },
            'aria-selected': value === input.dataset.value ? 'true' : 'false',
            onclick: () => {
              input.dataset.value = value;
              for (const b of input.children) {
                b.setAttribute('aria-selected', b.dataset.value === value ? 'true' : 'false');
              }
              input.dispatchEvent(new Event('change', { bubbles: true }));
            },
          }, o.label ?? o));
        }
      } else if (f.type === 'toggle') {
        input = el('button.toggle' + (f.value ? '.on' : ''), {
          type: 'button', role: 'switch', 'aria-checked': f.value ? 'true' : 'false',
          'aria-label': f.label,
          onclick: () => {
            const on = input.classList.toggle('on');
            input.setAttribute('aria-checked', on ? 'true' : 'false');
            input.dispatchEvent(new Event('change', { bubbles: true }));
          },
        }, el('span.knob'));
      } else {
        input = el('input.input' + (f.mono ? '.mono' : ''), {
          id: `dlg-${f.name}`, type: f.type || 'text', value: f.value ?? '',
          placeholder: f.placeholder || '', autocomplete: 'off', spellcheck: 'false',
        });
      }

      let row;
      if (f.type === 'toggle') {
        row = el('div.switch-row', {},
          el('div', {}, el('div.t', { text: f.label }), f.hint && el('div.d', { text: f.hint })),
          input);
      } else if (f.type === 'segmented') {
        row = el('div', {}, input);
      } else {
        row = el('div', {},
          el('label.field-label', { for: `dlg-${f.name}`, text: f.label }),
          input,
          f.hint && el('div.field-note', { text: f.hint }));
      }
      inputs.set(f.name, { field: f, input, row });
      body.append(row);
    }

    const error = el('div.modal-error', { role: 'alert', hidden: true });
    body.append(error);
    // The complaint is about the values as they were; any edit clears it.
    const clearError = () => { error.hidden = true; error.textContent = ''; };
    body.addEventListener('input', clearError);
    body.addEventListener('change', clearError);

    const opener = document.activeElement;
    const finish = (value) => {
      document.removeEventListener('keydown', onKey, true);
      backdrop.remove();
      // Put the keyboard back where it was.
      if (opener && document.contains(opener)) opener.focus?.();
      resolve(value);
    };

    const valueOf = ({ field, input }) =>
      field.type === 'toggle' ? input.classList.contains('on')
        : field.type === 'segmented' ? input.dataset.value
        : input.value;

    const submit = () => {
      const out = {};
      for (const [name, rec] of inputs) out[name] = valueOf(rec);
      // A validator keeps the dialog open and says why, so the Enter key and
      // the button behave the same.
      const problem = validate ? validate(out) : null;
      error.textContent = problem || '';
      error.hidden = !problem;
      if (problem) return;
      finish(out);
    };

    const onKey = (ev) => {
      if (ev.key === 'Escape') { ev.stopPropagation(); finish(null); }
      else if (ev.key === 'Enter' && ev.target.tagName !== 'TEXTAREA') { ev.stopPropagation(); submit(); }
    };

    const backdrop = el('div.backdrop', {
      onmousedown: (ev) => { if (ev.target === backdrop) finish(null); },
    }, el('div.modal', { role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      el('div.modal-head', {},
        el('h2.modal-title', { text: title }),
        description && el('div.modal-desc', { text: description }),
        detail && el('pre.modal-output', { text: detail })),
      fields.length ? body : el('div', { style: 'height:16px' }),
      el('div.modal-foot', {},
        cancelText && el('button.btn', { onclick: () => finish(null) }, cancelText),
        el(`button.btn.${danger ? 'btn-danger' : 'btn-primary'}`, { onclick: submit }, confirmText))));

    document.addEventListener('keydown', onKey, true);
    document.body.append(backdrop);

    if (onMount) {
      const rec = (name) => inputs.get(name);
      const get = (name) => {
        const r = rec(name);
        return r ? valueOf(r) : undefined;
      };
      const set = (name, value) => {
        const r = rec(name);
        if (!r) return;
        if (r.field.type === 'toggle') {
          r.input.classList.toggle('on', !!value);
          r.input.setAttribute('aria-checked', value ? 'true' : 'false');
        } else if (r.field.type === 'segmented') {
          r.input.dataset.value = value;
          for (const b of r.input.children) {
            b.setAttribute('aria-selected', b.dataset.value === value ? 'true' : 'false');
          }
        } else r.input.value = value ?? '';
      };
      const setOptions = (name, options, selected) => {
        const r = rec(name);
        if (!r || r.field.type !== 'select') return;
        fill(r.input, optionNodes(options, selected));
        if (selected != null) r.input.value = selected;
      };
      onMount({
        get, set, setOptions,
        input: (name) => rec(name)?.input,
        row: (name) => rec(name)?.row,
      });
    }

    const first = [...inputs.values()].find(
      (i) => i.field.type !== 'toggle' && i.field.type !== 'segmented');
    if (first) { first.input.focus(); first.input.select?.(); }
    // A fieldless confirmation still needs focus inside the dialog.
    else backdrop.querySelector('.modal-foot .btn:last-child')?.focus();
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
