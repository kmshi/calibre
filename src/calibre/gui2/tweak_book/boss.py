#!/usr/bin/env python
# vim:fileencoding=utf-8
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__ = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'

import tempfile, shutil, sys, os
from collections import OrderedDict
from functools import partial, wraps

from PyQt4.Qt import (
    QObject, QApplication, QDialog, QGridLayout, QLabel, QSize, Qt, QCursor,
    QDialogButtonBox, QIcon, QTimer, QPixmap, QTextBrowser, QVBoxLayout, QInputDialog)

from calibre import prints, prepare_string_for_xml, isbytestring
from calibre.ptempfile import PersistentTemporaryDirectory
from calibre.ebooks.oeb.base import urlnormalize
from calibre.ebooks.oeb.polish.main import SUPPORTED, tweak_polish
from calibre.ebooks.oeb.polish.container import get_container as _gc, clone_container, guess_type, OEB_FONTS
from calibre.ebooks.oeb.polish.cover import mark_as_cover, mark_as_titlepage
from calibre.ebooks.oeb.polish.pretty import fix_all_html, pretty_all
from calibre.ebooks.oeb.polish.replace import rename_files, replace_file
from calibre.ebooks.oeb.polish.split import split, merge, AbortError
from calibre.gui2 import error_dialog, choose_files, question_dialog, info_dialog, choose_save_file
from calibre.gui2.dialogs.confirm_delete import confirm
from calibre.gui2.tweak_book import set_current_container, current_container, tprefs, actions, editors
from calibre.gui2.tweak_book.undo import GlobalUndoHistory
from calibre.gui2.tweak_book.file_list import NewFileDialog
from calibre.gui2.tweak_book.save import SaveManager, save_container
from calibre.gui2.tweak_book.preview import parse_worker, font_cache
from calibre.gui2.tweak_book.toc import TOCEditor
from calibre.gui2.tweak_book.editor import editor_from_syntax, syntax_from_mime
from calibre.gui2.tweak_book.preferences import Preferences

def get_container(*args, **kwargs):
    kwargs['tweak_mode'] = True
    container = _gc(*args, **kwargs)
    # We preload the embedded fonts from this book, so that the preview panel
    # works
    font_cache.remove_fonts()
    for name, mt in container.mime_map.iteritems():
        if mt in OEB_FONTS and container.exists(name):
            with container.open(name, 'rb') as f:
                raw = f.read()
            font_cache.add_font(raw)
    return container

def setup_cssutils_serialization():
    import cssutils
    prefs = cssutils.ser.prefs
    prefs.indent = tprefs['editor_tab_stop_width'] * ' '
    prefs.indentClosingBrace = False
    prefs.omitLastSemicolon = False

class BusyCursor(object):

    def __enter__(self):
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))

    def __exit__(self, *args):
        QApplication.restoreOverrideCursor()

def in_thread_job(func):
    @wraps(func)
    def ans(*args, **kwargs):
        with BusyCursor():
            return func(*args, **kwargs)
    return ans

class Boss(QObject):

    def __init__(self, parent, notify=None):
        QObject.__init__(self, parent)
        self.global_undo = GlobalUndoHistory()
        self.container_count = 0
        self.tdir = None
        self.save_manager = SaveManager(parent, notify)
        self.save_manager.report_error.connect(self.report_save_error)
        self.doing_terminal_save = False
        self.ignore_preview_to_editor_sync = False
        setup_cssutils_serialization()

    def __call__(self, gui):
        self.gui = gui
        fl = gui.file_list
        fl.delete_requested.connect(self.delete_requested)
        fl.reorder_spine.connect(self.reorder_spine)
        fl.rename_requested.connect(self.rename_requested)
        fl.bulk_rename_requested.connect(self.bulk_rename_requested)
        fl.edit_file.connect(self.edit_file_requested)
        fl.merge_requested.connect(self.merge_requested)
        fl.mark_requested.connect(self.mark_requested)
        fl.export_requested.connect(self.export_requested)
        fl.replace_requested.connect(self.replace_requested)
        self.gui.central.current_editor_changed.connect(self.apply_current_editor_state)
        self.gui.central.close_requested.connect(self.editor_close_requested)
        self.gui.central.search_panel.search_triggered.connect(self.search)
        self.gui.preview.sync_requested.connect(self.sync_editor_to_preview)
        self.gui.preview.split_start_requested.connect(self.split_start_requested)
        self.gui.preview.split_requested.connect(self.split_requested)
        self.gui.preview.link_clicked.connect(self.link_clicked)
        self.gui.check_book.item_activated.connect(self.check_item_activated)
        self.gui.check_book.check_requested.connect(self.check_requested)
        self.gui.check_book.fix_requested.connect(self.fix_requested)
        self.gui.toc_view.navigate_requested.connect(self.link_clicked)

    def preferences(self):
        p = Preferences(self.gui)
        if p.exec_() == p.Accepted:
            for ed in editors.itervalues():
                ed.apply_settings()
            self.gui.keyboard.finalize()
            setup_cssutils_serialization()

    def mark_requested(self, name, action):
        self.commit_dirty_opf()
        c = current_container()
        if action == 'cover':
            mark_as_cover(current_container(), name)
        elif action.startswith('titlepage:'):
            action, move_to_start = action.partition(':')[0::2]
            move_to_start = move_to_start == 'True'
            mark_as_titlepage(current_container(), name, move_to_start=move_to_start)

        if c.opf_name in editors:
            editors[c.opf_name].replace_data(c.raw_data(c.opf_name))
        self.gui.file_list.build(c)
        self.set_modified()

    def mkdtemp(self, prefix=''):
        self.container_count += 1
        return tempfile.mkdtemp(prefix='%s%05d-' % (prefix, self.container_count), dir=self.tdir)

    def open_book(self, path=None, edit_file=None, clear_notify_data=True):
        if self.gui.action_save.isEnabled():
            if not question_dialog(self.gui, _('Unsaved changes'), _(
                'The current book has unsaved changes. If you open a new book, they will be lost'
                ' are you sure you want to proceed?')):
                return
        if self.save_manager.has_tasks:
            return info_dialog(self.gui, _('Cannot open'),
                        _('The current book is being saved, you cannot open a new book until'
                          ' the saving is completed'), show=True)

        if not hasattr(path, 'rpartition'):
            path = choose_files(self.gui, 'open-book-for-tweaking', _('Choose book'),
                                [(_('Books'), [x.lower() for x in SUPPORTED])], all_files=False, select_only_single_file=True)
            if not path:
                return
            path = path[0]

        ext = path.rpartition('.')[-1].upper()
        if ext not in SUPPORTED:
            return error_dialog(self.gui, _('Unsupported format'),
                _('Tweaking is only supported for books in the %s formats.'
                  ' Convert your book to one of these formats first.') % _(' and ').join(sorted(SUPPORTED)),
                show=True)
        if not os.path.exists(path):
            return error_dialog(self.gui, _('File not found'), _(
                'The file %s does not exist.') % path, show=True)

        for name in tuple(editors):
            self.close_editor(name)
        self.gui.preview.clear()
        self.container_count = -1
        if self.tdir:
            shutil.rmtree(self.tdir, ignore_errors=True)
        self.tdir = PersistentTemporaryDirectory()
        self._edit_file_on_open = edit_file
        self._clear_notify_data = clear_notify_data
        self.gui.blocking_job('open_book', _('Opening book, please wait...'), self.book_opened, get_container, path, tdir=self.mkdtemp())

    def book_opened(self, job):
        ef = getattr(self, '_edit_file_on_open', None)
        cn = getattr(self, '_clear_notify_data', True)
        self._edit_file_on_open = None

        if job.traceback is not None:
            if 'DRMError:' in job.traceback:
                from calibre.gui2.dialogs.drm_error import DRMErrorMessage
                return DRMErrorMessage(self.gui).exec_()
            return error_dialog(self.gui, _('Failed to open book'),
                    _('Failed to open book, click Show details for more information.'),
                                det_msg=job.traceback, show=True)
        if cn:
            self.save_manager.clear_notify_data()
        parse_worker.clear()
        container = job.result
        set_current_container(container)
        with BusyCursor():
            self.current_metadata = self.gui.current_metadata = container.mi
            self.global_undo.open_book(container)
            self.gui.update_window_title()
            self.gui.file_list.current_edited_name = None
            self.gui.file_list.build(container, preserve_state=False)
            self.gui.action_save.setEnabled(False)
            self.update_global_history_actions()
            recent_books = list(tprefs.get('recent-books', []))
            path = container.path_to_ebook
            if path in recent_books:
                recent_books.remove(path)
            recent_books.insert(0, path)
            tprefs['recent-books'] = recent_books[:10]
            self.gui.update_recent_books()
            if ef:
                self.gui.file_list.request_edit(ef)
            self.gui.toc_view.update_if_visible()

    def update_editors_from_container(self, container=None):
        c = container or current_container()
        for name, ed in tuple(editors.iteritems()):
            if c.has_name(name):
                ed.replace_data(c.raw_data(name))
                ed.is_synced_to_container = True
            else:
                self.close_editor(name)

    def apply_container_update_to_gui(self):
        container = current_container()
        self.gui.file_list.build(container)
        self.update_global_history_actions()
        self.update_editors_from_container()
        self.set_modified()
        self.gui.toc_view.update_if_visible()

    @in_thread_job
    def delete_requested(self, spine_items, other_items):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Delete files'))
        c = current_container()
        c.remove_from_spine(spine_items)
        for name in other_items:
            c.remove_item(name)
        self.set_modified()
        self.gui.file_list.delete_done(spine_items, other_items)
        for name in list(spine_items) + list(other_items):
            if name in editors:
                self.close_editor(name)
        if not editors:
            self.gui.preview.clear()

    def commit_dirty_opf(self):
        c = current_container()
        if c.opf_name in editors and not editors[c.opf_name].is_synced_to_container:
            self.commit_editor_to_container(c.opf_name)

    def reorder_spine(self, items):
        self.commit_dirty_opf()
        self.add_savepoint(_('Re-order text'))
        c = current_container()
        c.set_spine(items)
        self.set_modified()
        self.gui.file_list.build(current_container())  # needed as the linear flag may have changed on some items
        if c.opf_name in editors:
            editors[c.opf_name].replace_data(c.raw_data(c.opf_name))

    def add_file(self):
        if current_container() is None:
            return error_dialog(self.gui, _('No open book'), _(
                'You must first open a book to tweak, before trying to create new files'
                ' in it.'), show=True)

        self.commit_dirty_opf()
        d = NewFileDialog(self.gui)
        if d.exec_() != d.Accepted:
            return
        self.add_savepoint(_('Add file %s') % self.gui.elided_text(d.file_name))
        c = current_container()
        data = d.file_data
        if d.using_template:
            data = data.replace(b'%CURSOR%', b'')
        try:
            c.add_file(d.file_name, data)
        except:
            self.rewind_savepoint()
            raise
        self.gui.file_list.build(c)
        self.gui.file_list.select_name(d.file_name)
        if c.opf_name in editors:
            editors[c.opf_name].replace_data(c.raw_data(c.opf_name))
        mt = c.mime_map[d.file_name]
        syntax = syntax_from_mime(d.file_name, mt)
        if syntax:
            if d.using_template:
                self.edit_file(d.file_name, syntax, use_template=d.file_data.decode('utf-8'))
            else:
                self.edit_file(d.file_name, syntax)

    def edit_toc(self):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Edit Table of Contents'))
        d = TOCEditor(title=self.current_metadata.title, parent=self.gui)
        if d.exec_() != d.Accepted:
            self.rewind_savepoint()
            return
        with BusyCursor():
            self.update_editors_from_container()
            self.gui.toc_view.update_if_visible()

    def polish(self, action, name):
        self.commit_all_editors_to_container()
        with BusyCursor():
            self.add_savepoint(name)
            try:
                report = tweak_polish(current_container(), {action:True})
            except:
                self.rewind_savepoint()
                raise
            self.apply_container_update_to_gui()
            from calibre.ebooks.markdown import markdown
            report = markdown('# %s\n\n'%self.current_metadata.title + '\n\n'.join(report), output_format='html4')
        d = QDialog(self.gui)
        d.l = QVBoxLayout()
        d.setLayout(d.l)
        d.e = QTextBrowser(d)
        d.l.addWidget(d.e)
        d.e.setHtml(report)
        d.bb = QDialogButtonBox(QDialogButtonBox.Close)
        d.l.addWidget(d.bb)
        d.bb.rejected.connect(d.reject)
        d.bb.accepted.connect(d.accept)
        d.resize(600, 400)
        d.exec_()

    # Renaming {{{
    def rename_requested(self, oldname, newname):
        self.commit_all_editors_to_container()
        if guess_type(oldname) != guess_type(newname):
            args = os.path.splitext(oldname) + os.path.splitext(newname)
            if not confirm(
                _('You are changing the file type of {0}<b>{1}</b> to {2}<b>{3}</b>.'
                  ' Doing so can cause problems, are you sure?').format(*args),
                'confirm-filetype-change', parent=self.gui, title=_('Are you sure?'),
                config_set=tprefs):
                return
        if urlnormalize(newname) != newname:
            if not confirm(
                _('The name you have chosen {0} contains special characters, internally'
                  ' it will look like: {1}Try to use only the English alphabet [a-z], numbers [0-9],'
                  ' hyphens and underscores for file names. Other characters can cause problems for '
                  ' different ebook viewers. Are you sure you want to proceed?').format(
                      '<pre>%s</pre>'%newname, '<pre>%s</pre>' % urlnormalize(newname)),
                'confirm-urlunsafe-change', parent=self.gui, title=_('Are you sure?'), config_set=tprefs):
                    return
        self.add_savepoint(_('Rename %s') % oldname)
        name_map = {oldname:newname}
        self.gui.blocking_job(
            'rename_file', _('Renaming and updating links...'), partial(self.rename_done, name_map),
            rename_files, current_container(), name_map)

    def bulk_rename_requested(self, name_map):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Bulk rename'))
        self.gui.blocking_job(
            'bulk_rename_files', _('Renaming and updating links...'), partial(self.rename_done, name_map),
            rename_files, current_container(), name_map)

    def rename_done(self, name_map, job):
        if job.traceback is not None:
            return error_dialog(self.gui, _('Failed to rename files'),
                    _('Failed to rename files, click Show details for more information.'),
                                det_msg=job.traceback, show=True)
        self.gui.file_list.build(current_container())
        self.set_modified()
        for oldname, newname in name_map.iteritems():
            if oldname in editors:
                editors[newname] = editors.pop(oldname)
                self.gui.central.rename_editor(editors[newname], newname)
        self.apply_container_update_to_gui()
    # }}}

    # Global history {{{
    def do_global_undo(self):
        container = self.global_undo.undo()
        if container is not None:
            set_current_container(container)
            self.apply_container_update_to_gui()

    def do_global_redo(self):
        container = self.global_undo.redo()
        if container is not None:
            set_current_container(container)
            self.apply_container_update_to_gui()

    def update_global_history_actions(self):
        gu = self.global_undo
        for x, text in (('undo', _('&Revert to before')), ('redo', '&Revert to after')):
            ac = getattr(self.gui, 'action_global_%s' % x)
            ac.setEnabled(getattr(gu, 'can_' + x))
            ac.setText(text + ' ' + (getattr(gu, x + '_msg') or '...'))

    def add_savepoint(self, msg):
        nc = clone_container(current_container(), self.mkdtemp())
        self.global_undo.add_savepoint(nc, msg)
        set_current_container(nc)
        self.update_global_history_actions()

    def rewind_savepoint(self):
        container = self.global_undo.rewind_savepoint()
        if container is not None:
            set_current_container(container)
            self.update_global_history_actions()
    # }}}

    def set_modified(self):
        self.gui.action_save.setEnabled(True)

    def fix_html(self, current):
        if current:
            ed = self.gui.central.current_editor
            if hasattr(ed, 'fix_html'):
                ed.fix_html()
        else:
            self.commit_all_editors_to_container()
            with BusyCursor():
                self.add_savepoint(_('Fix HTML'))
                fix_all_html(current_container())
                self.update_editors_from_container()
                self.set_modified()

    def pretty_print(self, current):
        if current:
            ed = self.gui.central.current_editor
            for name, x in editors.iteritems():
                if x is ed:
                    break
            ed.pretty_print(name)
        else:
            self.commit_all_editors_to_container()
            with BusyCursor():
                self.add_savepoint(_('Beautify files'))
                pretty_all(current_container())
                self.update_editors_from_container()
                self.set_modified()

    def mark_selected_text(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.mark_selected_text()
            if ed.has_marked_text:
                self.gui.central.search_panel.set_where('selected-text')

    def show_find(self):
        self.gui.central.show_find()
        ed = self.gui.central.current_editor
        if ed is not None and hasattr(ed, 'selected_text'):
            text = ed.selected_text
            if text and text.strip():
                self.gui.central.pre_fill_search(text)

    def search(self, action, overrides=None):
        ' Run a search/replace '
        sp = self.gui.central.search_panel
        # Ensure the search panel is visible
        sp.setVisible(True)
        ed = self.gui.central.current_editor
        name = editor = None
        for n, x in editors.iteritems():
            if x is ed:
                name = n
                break
        state = sp.state
        if overrides:
            state.update(overrides)
        searchable_names = self.gui.file_list.searchable_names
        where = state['where']
        err = None
        if name is None and where in {'current', 'selected-text'}:
            err = _('No file is being edited.')
        elif where == 'selected' and not searchable_names['selected']:
            err = _('No files are selected in the Files Browser')
        elif where == 'selected-text' and not ed.has_marked_text:
            err = _('No text is marked. First select some text, and then use'
                    ' The "Mark selected text" action in the Search menu to mark it.')
        if not err and not state['find']:
            err = _('No search query specified')
        if err:
            return error_dialog(self.gui, _('Cannot search'), err, show=True)
        del err

        files = OrderedDict()
        do_all = state['wrap'] or action in {'replace-all', 'count'}
        marked = False
        if where == 'current':
            editor = ed
        elif where in {'styles', 'text', 'selected'}:
            files = searchable_names[where]
            if name in files:
                # Start searching in the current editor
                editor = ed
                # Re-order the list of other files so that we search in the same
                # order every time. Depending on direction, search the files
                # that come after the current file, or before the current file,
                # first.
                lfiles = list(files)
                idx = lfiles.index(name)
                before, after = lfiles[:idx], lfiles[idx+1:]
                if state['direction'] == 'up':
                    lfiles = list(reversed(before))
                    if do_all:
                        lfiles += list(reversed(after)) + [name]
                else:
                    lfiles = after
                    if do_all:
                        lfiles += before + [name]
                files = OrderedDict((m, files[m]) for m in lfiles)
        else:
            editor = ed
            marked = True

        def no_match():
            QApplication.restoreOverrideCursor()
            msg = '<p>' + _('No matches were found for %s.') % prepare_string_for_xml(state['find'])
            if not state['wrap']:
                msg += '<p>' + _('You have turned off search wrapping, so all text might not have been searched.'
                  ' Try the search again, with wrapping enabled. Wrapping is enabled via the'
                  ' "Wrap" checkbox at the bottom of the search panel.')
            return error_dialog(
                self.gui, _('Not found'), msg, show=True)

        pat = sp.get_regex(state)

        def do_find():
            if editor is not None:
                if editor.find(pat, marked=marked):
                    return
                if not files:
                    if not state['wrap']:
                        return no_match()
                    return editor.find(pat, wrap=True, marked=marked) or no_match()
            for fname, syntax in files.iteritems():
                if fname in editors:
                    if not editors[fname].find(pat, complete=True):
                        continue
                    return self.show_editor(fname)
                raw = current_container().raw_data(fname)
                if pat.search(raw) is not None:
                    self.edit_file(fname, syntax)
                    if editors[fname].find(pat, complete=True):
                        return
            return no_match()

        def no_replace(prefix=''):
            QApplication.restoreOverrideCursor()
            if prefix:
                prefix += ' '
            error_dialog(
                self.gui, _('Cannot replace'), prefix + _(
                'You must first click Find, before trying to replace'), show=True)
            return False

        def do_replace():
            if editor is None:
                return no_replace()
            if not editor.replace(pat, state['replace']):
                return no_replace(_(
                        'Currently selected text does not match the search query.'))
            return True

        def count_message(action, count):
            msg = _('%(action)s %(num)s occurrences of %(query)s' % dict(num=count, query=state['find'], action=action))
            info_dialog(self.gui, _('Searching done'), prepare_string_for_xml(msg), show=True)

        def do_all(replace=True):
            count = 0
            if not files and editor is None:
                return 0
            lfiles = files or {name:editor.syntax}

            for n, syntax in lfiles.iteritems():
                if n in editors:
                    raw = editors[n].get_raw_data()
                else:
                    raw = current_container().raw_data(n)
                if replace:
                    raw, num = pat.subn(state['replace'], raw)
                else:
                    num = len(pat.findall(raw))
                count += num
                if replace and num > 0:
                    if n in editors:
                        editors[n].replace_data(raw)
                    else:
                        with current_container().open(n, 'wb') as f:
                            f.write(raw.encode('utf-8'))
            QApplication.restoreOverrideCursor()
            count_message(_('Replaced') if replace else _('Found'), count)
            return count

        with BusyCursor():
            if action == 'find':
                return do_find()
            if action == 'replace':
                return do_replace()
            if action == 'replace-find' and do_replace():
                return do_find()
            if action == 'replace-all':
                if marked:
                    return count_message(_('Replaced'), editor.all_in_marked(pat, state['replace']))
                self.add_savepoint(_('Replace all'))
                count = do_all()
                if count == 0:
                    self.rewind_savepoint()
                return
            if action == 'count':
                if marked:
                    return count_message(_('Found'), editor.all_in_marked(pat))
                return do_all(replace=False)

    def create_checkpoint(self):
        text, ok = QInputDialog.getText(self.gui, _('Choose name'), _(
            'Choose a name for the checkpoint.\nYou can later restore the book'
            ' to this checkpoint via the\n"Revert to..." entries in the Edit menu.'))
        if ok:
            self.add_savepoint(text)

    def commit_editor_to_container(self, name, container=None):
        container = container or current_container()
        ed = editors[name]
        with container.open(name, 'wb') as f:
            f.write(ed.data)
            if container is current_container():
                ed.is_synced_to_container = True

    def commit_all_editors_to_container(self):
        with BusyCursor():
            for name, ed in editors.iteritems():
                if not ed.is_synced_to_container:
                    self.commit_editor_to_container(name)
                    ed.is_synced_to_container = True

    def save_book(self):
        c = current_container()
        for name, ed in editors.iteritems():
            if ed.is_modified or not ed.is_synced_to_container:
                self.commit_editor_to_container(name, c)
                ed.is_modified = False
        self.gui.action_save.setEnabled(False)
        tdir = self.mkdtemp(prefix='save-')
        container = clone_container(c, tdir)
        self.save_manager.schedule(tdir, container)

    def save_copy(self):
        c = current_container()
        ext = c.path_to_ebook.rpartition('.')[-1]
        path = choose_save_file(self.gui, 'tweak_book_save_copy', _(
            'Choose path'), filters=[(_('Book (%s)') % ext.upper(), [ext.lower()])], all_files=False)
        if not path:
            return
        tdir = self.mkdtemp(prefix='save-copy-')
        container = clone_container(c, tdir)
        for name, ed in editors.iteritems():
            if ed.is_modified or not ed.is_synced_to_container:
                self.commit_editor_to_container(name, container)

        def do_save(c, path, tdir):
            save_container(c, path)
            shutil.rmtree(tdir, ignore_errors=True)
            return path

        self.gui.blocking_job('save_copy', _('Saving copy, please wait...'), self.copy_saved, do_save, container, path, tdir)

    def copy_saved(self, job):
        if job.traceback is not None:
            return error_dialog(self.gui, _('Failed to save copy'),
                    _('Failed to save copy, click Show details for more information.'), det_msg=job.traceback, show=True)
        msg = _('Copy saved to %s') % job.result
        info_dialog(self.gui, _('Copy saved'), msg, show=True)
        self.gui.show_status_message(msg, 5)

    def report_save_error(self, tb):
        if self.doing_terminal_save:
            prints(tb, file=sys.stderr)
            return
        error_dialog(self.gui, _('Could not save'),
                     _('Saving of the book failed. Click "Show Details"'
                       ' for more information.'), det_msg=tb, show=True)

    def go_to_line_number(self):
        ed = self.gui.central.current_editor
        if ed is None or not ed.has_line_numbers:
            return
        num, ok = QInputDialog.getInt(self.gui, _('Enter line number'), ('Line number:'), ed.current_line, 1, max(100000, ed.number_of_lines))
        if ok:
            ed.current_line = num

    def split_start_requested(self):
        self.commit_all_editors_to_container()
        self.gui.preview.do_start_split()

    @in_thread_job
    def split_requested(self, name, loc):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Split %s') % self.gui.elided_text(name))
        try:
            bottom_name = split(current_container(), name, loc)
        except AbortError:
            self.rewind_savepoint()
            raise
        self.apply_container_update_to_gui()
        self.edit_file(bottom_name, 'html')

    @in_thread_job
    def link_clicked(self, name, anchor):
        if not name:
            return
        if name in editors:
            editor = editors[name]
            self.gui.central.show_editor(editor)
        else:
            syntax = syntax_from_mime(name, current_container().mime_map[name])
            editor = self.edit_file(name, syntax)
        if anchor:
            editor.go_to_anchor(anchor)

    @in_thread_job
    def check_item_activated(self, item):
        name = item.name
        if name in editors:
            editor = editors[name]
            self.gui.central.show_editor(editor)
        else:
            editor = self.edit_file_requested(name, None, current_container().mime_map[name])
        if getattr(editor, 'has_line_numbers', False):
            editor.go_to_line(item.line, item.col)
            editor.set_focus()

    @in_thread_job
    def check_requested(self, *args):
        self.commit_all_editors_to_container()
        c = self.gui.check_book
        c.parent().show()
        c.parent().raise_()
        c.run_checks(current_container())

    @in_thread_job
    def fix_requested(self, errors):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Auto-fix errors'))
        c = self.gui.check_book
        c.parent().show()
        c.parent().raise_()
        changed = c.fix_errors(current_container(), errors)
        if changed:
            self.apply_container_update_to_gui()
            self.set_modified()
        else:
            self.rewind_savepoint()

    @in_thread_job
    def merge_requested(self, category, names, master):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Merge files into %s') % self.gui.elided_text(master))
        try:
            merge(current_container(), category, names, master)
        except AbortError:
            self.rewind_savepoint()
            raise
        self.apply_container_update_to_gui()
        if master in editors:
            self.show_editor(master)

    @in_thread_job
    def export_requested(self, name, path):
        if name in editors and not editors[name].is_synced_to_container:
            self.commit_editor_to_container(name)
        with current_container().open(name, 'rb') as src, open(path, 'wb') as dest:
            shutil.copyfileobj(src, dest)

    @in_thread_job
    def replace_requested(self, name, path, basename, force_mt):
        self.commit_all_editors_to_container()
        self.add_savepoint(_('Replace %s') % name)
        replace_file(current_container(), name, path, basename, force_mt)
        self.apply_container_update_to_gui()

    def sync_editor_to_preview(self, name, lnum):
        editor = self.edit_file(name, 'html')
        self.ignore_preview_to_editor_sync = True
        try:
            editor.current_line = lnum
        finally:
            self.ignore_preview_to_editor_sync = False

    def sync_preview_to_editor(self):
        if self.ignore_preview_to_editor_sync:
            return
        ed = self.gui.central.current_editor
        if ed is not None:
            name = None
            for n, x in editors.iteritems():
                if ed is x:
                    name = n
                    break
            if name is not None and getattr(ed, 'syntax', None) == 'html':
                self.gui.preview.sync_to_editor(name, ed.current_line)

    def init_editor(self, name, editor, data=None, use_template=False):
        editor.undo_redo_state_changed.connect(self.editor_undo_redo_state_changed)
        editor.data_changed.connect(self.editor_data_changed)
        editor.copy_available_state_changed.connect(self.editor_copy_available_state_changed)
        editor.cursor_position_changed.connect(self.sync_preview_to_editor)
        editor.cursor_position_changed.connect(self.update_cursor_position)
        if data is not None:
            if use_template:
                editor.init_from_template(data)
            else:
                editor.data = data
                editor.is_synced_to_container = True
        editor.modification_state_changed.connect(self.editor_modification_state_changed)
        self.gui.central.add_editor(name, editor)

    def edit_file(self, name, syntax, use_template=None):
        editor = editors.get(name, None)
        if editor is None:
            if use_template is None:
                data = current_container().raw_data(name)
                if isbytestring(data) and syntax in {'html', 'css', 'text', 'xml'}:
                    try:
                        data = data.decode('utf-8')
                    except UnicodeDecodeError:
                        return error_dialog(self.gui, _('Cannot decode'), _(
                            'Cannot edit %s as it appears to be in an unknown character encoding') % name, show=True)
            else:
                data = use_template
            editor = editors[name] = editor_from_syntax(syntax, self.gui.editor_tabs)
            self.init_editor(name, editor, data, use_template=bool(use_template))
        self.show_editor(name)
        return editor

    def show_editor(self, name):
        self.gui.central.show_editor(editors[name])
        editors[name].set_focus()

    def edit_file_requested(self, name, syntax, mime):
        if name in editors:
            self.gui.central.show_editor(editors[name])
            return
        syntax = syntax or syntax_from_mime(name, mime)
        if not syntax:
            return error_dialog(
                self.gui, _('Unsupported file format'),
                _('Editing files of type %s is not supported' % mime), show=True)
        return self.edit_file(name, syntax)

    # Editor basic controls {{{
    def do_editor_undo(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.undo()

    def do_editor_redo(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.redo()

    def do_editor_copy(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.copy()

    def do_editor_cut(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.cut()

    def do_editor_paste(self):
        ed = self.gui.central.current_editor
        if ed is not None:
            ed.paste()

    def editor_data_changed(self, editor):
        self.gui.preview.start_refresh_timer()

    def editor_undo_redo_state_changed(self, *args):
        self.apply_current_editor_state()

    def editor_copy_available_state_changed(self, *args):
        self.apply_current_editor_state()

    def editor_modification_state_changed(self, is_modified):
        self.apply_current_editor_state()
        if is_modified:
            self.set_modified()
    # }}}

    def apply_current_editor_state(self):
        ed = self.gui.central.current_editor
        self.gui.cursor_position_widget.update_position()
        if ed is not None:
            actions['editor-undo'].setEnabled(ed.undo_available)
            actions['editor-redo'].setEnabled(ed.redo_available)
            actions['editor-copy'].setEnabled(ed.copy_available)
            actions['editor-cut'].setEnabled(ed.cut_available)
            actions['go-to-line-number'].setEnabled(ed.has_line_numbers)
            actions['fix-html-current'].setEnabled(ed.syntax == 'html')
            name = None
            for n, x in editors.iteritems():
                if ed is x:
                    name = n
                    break
            if name is not None and getattr(ed, 'syntax', None) == 'html':
                if self.gui.preview.show(name):
                    # The file being displayed by the preview has changed.
                    # Set the preview's position to the current cursor
                    # position in the editor, in case the editors' cursor
                    # position has not changed, since the last time it was
                    # focused. This is not inefficient since multiple requests
                    # to sync are de-bounced with a 100 msec wait.
                    self.sync_preview_to_editor()
            if name is not None:
                self.gui.file_list.mark_name_as_current(name)
            if ed.has_line_numbers:
                self.gui.cursor_position_widget.update_position(*ed.cursor_position)
        else:
            actions['go-to-line-number'].setEnabled(False)
            self.gui.file_list.clear_currently_edited_name()

    def update_cursor_position(self):
        ed = self.gui.central.current_editor
        if getattr(ed, 'has_line_numbers', False):
            self.gui.cursor_position_widget.update_position(*ed.cursor_position)
        else:
            self.gui.cursor_position_widget.update_position()

    def editor_close_requested(self, editor):
        name = None
        for n, ed in editors.iteritems():
            if ed is editor:
                name = n
        if not name:
            return
        if not editor.is_synced_to_container:
            self.commit_editor_to_container(name)
        self.close_editor(name)

    def close_editor(self, name):
        editor = editors.pop(name)
        self.gui.central.close_editor(editor)
        editor.break_cycles()
        if not editors:
            self.gui.preview.clear()

    # Shutdown {{{
    def quit(self):
        if not self.confirm_quit():
            return
        self.save_state()
        QApplication.instance().quit()

    def confirm_quit(self):
        if self.doing_terminal_save:
            return False
        if self.save_manager.has_tasks:
            if not question_dialog(
                self.gui, _('Are you sure?'), _(
                    'The current book is being saved in the background, quitting will abort'
                    ' the save process, are you sure?'), default_yes=False):
                return False

        if self.gui.action_save.isEnabled():
            d = QDialog(self.gui)
            d.l = QGridLayout(d)
            d.setLayout(d.l)
            d.setWindowTitle(_('Unsaved changes'))
            d.i = QLabel('')
            d.i.setPixmap(QPixmap(I('save.png')).scaledToHeight(64, Qt.SmoothTransformation))
            d.i.setMaximumSize(QSize(d.i.pixmap().width(), 64))
            d.i.setScaledContents(True)
            d.l.addWidget(d.i, 0, 0)
            d.m = QLabel(_('There are unsaved changes, if you quit without saving, you will lose them.'))
            d.m.setWordWrap(True)
            d.l.addWidget(d.m, 0, 1)
            d.bb = QDialogButtonBox(QDialogButtonBox.Cancel)
            d.bb.rejected.connect(d.reject)
            d.bb.accepted.connect(d.accept)
            d.l.addWidget(d.bb, 1, 0, 1, 2)
            d.do_save = None
            def endit(x):
                d.do_save = x
                d.accept()
            b = d.bb.addButton(_('&Save and Quit'), QDialogButtonBox.ActionRole)
            b.setIcon(QIcon(I('save.png')))
            b.clicked.connect(lambda *args: endit(True))
            b = d.bb.addButton(_('&Quit without saving'), QDialogButtonBox.ActionRole)
            b.clicked.connect(lambda *args: endit(False))
            d.resize(d.sizeHint())
            if d.exec_() != d.Accepted or d.do_save is None:
                return False
            if d.do_save:
                self.gui.action_save.trigger()
                self.gui.blocking_job.set_msg(_('Saving, please wait...'))
                self.gui.blocking_job.start()
                self.doing_terminal_save = True
                QTimer.singleShot(50, self.check_terminal_save)
                return False

        return True

    def check_terminal_save(self):
        if self.save_manager.has_tasks:
            return QTimer.singleShot(50, self.check_terminal_save)
        self.shutdown()
        QApplication.instance().quit()

    def shutdown(self):
        self.gui.preview.stop_refresh_timer()
        self.save_state()
        self.save_manager.shutdown()
        parse_worker.shutdown()
        self.save_manager.wait(0.1)

    def save_state(self):
        with tprefs:
            self.gui.save_state()
    # }}}

