/**
 * Ada-SI skill SDK for custom iframe mini-apps.
 * Load from /static/skill-sdk.js in generated index.html.
 */
(function (global) {
  'use strict';

  function parseSkillNameFromPath() {
    var parts = (global.location.pathname || '').split('/').filter(Boolean);
    var idx = parts.indexOf('skills');
    if (idx !== -1 && parts[idx + 1]) {
      return decodeURIComponent(parts[idx + 1]);
    }
    return null;
  }

  function AdaSkill() {
    this.skillName = null;
    this.actions = {};
    this._listeners = [];
  }

  AdaSkill.prototype.init = function (options) {
    options = options || {};
    this.skillName = options.skillName || parseSkillNameFromPath();
    if (!this.skillName) {
      throw new Error('AdaSkill: could not determine skill name');
    }
    this.actions = options.actions || {};
    return this;
  };

  AdaSkill.prototype._ensureInit = function () {
    if (!this.skillName) {
      this.init();
    }
  };

  AdaSkill.prototype._notifyListeners = function () {
    var self = this;
    self._listeners.forEach(function (cb) {
      try {
        cb();
      } catch (e) {
        console.error('AdaSkill listener error', e);
      }
    });
  };

  AdaSkill.prototype._url = function (path) {
    return path;
  };

  AdaSkill.prototype.getData = function () {
    var self = this;
    self._ensureInit();
    return fetch(self._url('/api/skills/' + encodeURIComponent(self.skillName) + '/data'))
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (t) {
            throw new Error(t || res.statusText);
          });
        }
        return res.json();
      });
  };

  AdaSkill.prototype.call = function (action, params) {
    var self = this;
    self._ensureInit();
    if (action && typeof action === 'object' && !Array.isArray(action)) {
      params = action;
      action = params.action;
      params = Object.assign({}, params);
      delete params.action;
    }
    if (!action || typeof action !== 'string') {
      return Promise.reject(new Error('AdaSkill.call requires an action name string'));
    }
    var body = Object.assign({ action: action }, params || {});
    return fetch(self._url('/api/skills/' + encodeURIComponent(self.skillName) + '/action'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var err = (data && data.detail && data.detail.error) || data.detail || res.statusText;
          throw new Error(typeof err === 'string' ? err : JSON.stringify(err));
        }
        self._notifyListeners();
        return data;
      });
    });
  };

  AdaSkill.prototype.action = function (key, params) {
    var actionName = this.actions[key];
    if (!actionName) {
      return Promise.reject(new Error('AdaSkill: no action mapped for key ' + key));
    }
    return this.call(actionName, params);
  };

  AdaSkill.prototype.create = function (params) {
    return this.action('create', params);
  };

  AdaSkill.prototype.delete = function (params) {
    return this.action('delete', params);
  };

  AdaSkill.prototype.toggle = function (params) {
    return this.action('toggle', params);
  };

  AdaSkill.prototype.fetch = function (params) {
    if (this.actions.fetch) {
      return this.action('fetch', params);
    }
    return this.getData();
  };

  AdaSkill.prototype.onDataChanged = function (callback) {
    if (typeof callback !== 'function') return;
    this._listeners.push(callback);
    var self = this;
    if (!this._messageBound) {
      this._messageBound = true;
      global.addEventListener('message', function (event) {
        if (!event.data || event.data.type !== 'ada:skill_data_changed') return;
        if (event.data.skillName && event.data.skillName !== self.skillName) return;
        self._listeners.forEach(function (cb) {
          try {
            cb();
          } catch (e) {
            console.error('AdaSkill listener error', e);
          }
        });
      });
    }
  };

  AdaSkill.prototype.loadActionsFromTools = function () {
    var self = this;
    self._ensureInit();
    return fetch('/api/tools')
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        var tools = data.tools || [];
        var tool = tools.find(function (t) {
          return t.name === self.skillName;
        });
        if (tool && tool.ui && tool.ui.actions) {
          self.actions = tool.ui.actions;
        }
        return self.actions;
      });
  };

  var instance = new AdaSkill();
  global.AdaSkill = instance;
})(typeof window !== 'undefined' ? window : globalThis);
