#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2012, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import copy
from functools import partial
from collections import defaultdict, namedtuple
from io import BytesIO
from struct import pack

import cssutils
from lxml import etree

from calibre import isbytestring, force_unicode
from calibre.ebooks.mobi.utils import create_text_record, to_base
from calibre.ebooks.compression.palmdoc import compress_doc
from calibre.ebooks.oeb.base import (OEB_DOCS, OEB_STYLES, SVG_MIME, XPath,
        extract, XHTML, urlnormalize)
from calibre.ebooks.oeb.parse_utils import barename
from calibre.ebooks.mobi.writer8.skeleton import Chunker, aid_able_tags, to_href

XML_DOCS = OEB_DOCS | {SVG_MIME}

# References to record numbers in KF8 are stored as base-32 encoded integers,
# with 4 digits
to_ref = partial(to_base, base=32, min_num_digits=4)

class KF8Writer(object):

    def __init__(self, oeb, opts, resources):
        self.oeb, self.opts, self.log = oeb, opts, oeb.log
        self.compress = not self.opts.dont_compress
        self.log.info('Creating KF8 output')
        self.used_images = set()
        self.resources = resources
        self.dup_data()
        self.flows = [None] # First flow item is reserved for the text
        self.records = []
        self.fdst_table = []

        self.replace_resource_links()
        self.extract_css_into_flows()
        self.extract_svg_into_flows()
        self.replace_internal_links_with_placeholders()
        self.insert_aid_attributes()
        self.chunk_it_up()
        # Dump the cloned data as it is no longer needed
        del self._data_cache
        self.create_text_records()
        self.create_fdst_table()

    def dup_data(self):
        ''' Duplicate data so that any changes we make to markup/CSS only
        affect KF8 output and not MOBI 6 output '''
        self._data_cache = {}
        for item in self.oeb.manifest:
            if item.media_type in XML_DOCS:
                self._data_cache[item.href] = copy.deepcopy(item.data)
            elif item.media_type in OEB_STYLES:
                # I can't figure out how to make an efficient copy of the
                # in-memory CSSStylesheet, as deepcopy doesn't work (raises an
                # exception)
                self._data_cache[item.href] = cssutils.parseString(
                        item.data.cssText)

    def data(self, item):
        return self._data_cache.get(item.href, item.data)

    def replace_resource_links(self):
        ''' Replace links to resources (raster images/fonts) with pointers to
        the MOBI record containing the resource. The pointers are of the form:
        kindle:embed:XXXX?mime=image/* The ?mime= is apparently optional and
        not used for fonts. '''

        def pointer(item, oref):
            ref = item.abshref(oref)
            idx = self.resources.item_map.get(ref, None)
            if idx is not None:
                is_image = self.resources.records[idx-1][:4] not in {b'FONT'}
                idx = to_ref(idx)
                if is_image:
                    self.used_images.add(ref)
                    return 'kindle:embed:%s?mime=%s'%(idx,
                            self.resources.mime_map[ref])
                else:
                    return 'kindle:embed:%s'%idx
            return oref

        for item in self.oeb.manifest:

            if item.media_type in XML_DOCS:
                root = self.data(item)
                for tag in XPath('//h:img|//svg:image')(root):
                    for attr, ref in tag.attrib.iteritems():
                        if attr.split('}')[-1].lower() in {'src', 'href'}:
                            tag.attrib[attr] = pointer(item, ref)

                for tag in XPath('//h:style')(root):
                    if tag.text:
                        sheet = cssutils.parseString(tag.text)
                        replacer = partial(pointer, item)
                        cssutils.replaceUrls(sheet, replacer,
                                ignoreImportRules=True)
                        repl = sheet.cssText
                        if isbytestring(repl):
                            repl = repl.decode('utf-8')
                        tag.text = '\n'+ repl + '\n'

            elif item.media_type in OEB_STYLES:
                sheet = self.data(item)
                replacer = partial(pointer, item)
                cssutils.replaceUrls(sheet, replacer, ignoreImportRules=True)

    def extract_css_into_flows(self):
        inlines = defaultdict(list) # Ensure identical <style>s not repeated
        sheets = {}

        for item in self.oeb.manifest:
            if item.media_type in OEB_STYLES:
                data = self.data(item).cssText
                self.flows.append(force_unicode(data, 'utf-8'))
                sheets[item.href] = len(self.flows)

        for item in self.oeb.spine:
            root = self.data(item)

            for link in XPath('//h:link[@href]')(root):
                href = item.abshref(link.get('href'))
                idx = sheets.get(href, None)
                if idx is not None:
                    idx = to_ref(idx)
                    link.set('href', 'kindle:flow:%s?mime=text/css'%idx)

            for tag in XPath('//h:style')(root):
                p = tag.getparent()
                idx = p.index(tag)
                raw = tag.text
                if not raw or not raw.strip():
                    extract(tag)
                    continue
                repl = etree.Element(XHTML('link'), type='text/css',
                        rel='stylesheet')
                repl.tail='\n'
                p.insert(idx, repl)
                extract(tag)
                inlines[raw].append(repl)

        for raw, elems in inlines.iteritems():
            self.flows.append(raw)
            idx = to_ref(len(self.flows))
            for link in elems:
                link.set('href', 'kindle:flow:%s?mime=text/css'%idx)

    def extract_svg_into_flows(self):
        for item in self.oeb.spine:
            root = self.data(item)

            for svg in XPath('//svg:svg')(root):
                raw = etree.tostring(svg, encoding=unicode, with_tail=False)
                self.flows.append(raw)
                p = svg.getparent()
                pos = p.index(svg)
                img = etree.Element(XHTML('img'),
                        src="kindle:flow:%s?mime=image/svg+xml"%to_ref(
                            len(self.flows)))
                p.insert(pos, img)
                extract(svg)

    def replace_internal_links_with_placeholders(self):
        self.link_map = {}
        count = 0
        hrefs = {item.href for item in self.oeb.spine}
        for item in self.oeb.spine:
            root = self.data(item)

            for a in XPath('//h:a[@href]')(root):
                count += 1
                ref = item.abshref(a.get('href'))
                href, _, frag = ref.partition('#')
                href = urlnormalize(href)
                if href in hrefs:
                    placeholder = 'kindle:pos:fid:0000:off:%s'%to_href(count)
                    self.link_map[placeholder] = (href, frag)
                    a.set('href', placeholder)

    def insert_aid_attributes(self):
        self.id_map = {}
        for i, item in enumerate(self.oeb.spine):
            root = self.data(item)
            aidbase = i * int(1e6)
            j = 0
            for tag in root.iterdescendants(etree.Element):
                id_ = tag.attrib.get('id', None)
                if id_ is not None or barename(tag.tag).lower() in aid_able_tags:
                    aid = aidbase + j
                    tag.attrib['aid'] = to_base(aid, base=32)
                    if tag.tag == XHTML('body'):
                        self.id_map[(item.href, '')] = tag.attrib['aid']
                    if id_ is not None:
                        self.id_map[(item.href, id_)] = tag.attrib['aid']

                    j += 1

    def chunk_it_up(self):
        placeholder_map = {}
        for placeholder, x in self.link_map.iteritems():
            href, frag = x
            aid = self.id_map.get(x, None)
            if aid is None:
                aid = self.id_map.get((href, ''))
            placeholder_map[placeholder] = aid
        chunker = Chunker(self.oeb, self.data, placeholder_map)

        for x in ('skel_table', 'chunk_table', 'aid_offset_map'):
            setattr(self, x, getattr(chunker, x))

        self.flows[0] = chunker.text

    def create_text_records(self):
        self.flows = [x.encode('utf-8') if isinstance(x, unicode) else x for x
                in self.flows]
        text = b''.join(self.flows)
        self.text_length = len(text)
        text = BytesIO(text)
        nrecords = 0
        records_size = 0

        if self.compress:
            self.oeb.logger.info('  Compressing markup content...')

        while text.tell() < self.text_length:
            data, overlap = create_text_record(text)
            if self.compress:
                data = compress_doc(data)

            data += overlap
            data += pack(b'>B', len(overlap))

            self.records.append(data)
            records_size += len(data)
            nrecords += 1

        self.last_text_record_idx = nrecords
        self.first_non_text_record_idx = nrecords + 1
        # Pad so that the next records starts at a 4 byte boundary
        if records_size % 4 != 0:
            self.records.append(b'\x00'*(records_size % 4))
            self.first_non_text_record_idx += 1

    def create_fdst_table(self):
        FDST = namedtuple('Flow', 'start end')
        for i, flow in enumerate(self.flows):
            start = 0 if i == 0 else self.fdst_table[-1].end
            self.fdst_table.append(FDST(start, start + len(flow)))
