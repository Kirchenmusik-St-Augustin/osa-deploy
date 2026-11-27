// Gives the panel GoAccess calls "remote user" the name the statistics use
// for it: the filter writes the kind of automated access into that field.
// Runs before GoAccess builds the page on window.onload, so the new name is
// already there when the panel is drawn.
(function () {
  "use strict";
  var panels = window.user_interface;
  if (!panels || !panels.remote_user) {
    return;
  }
  panels.remote_user.head = "Art des Zugriffs";
}());
