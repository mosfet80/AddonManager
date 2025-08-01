# SPDX-License-Identifier: LGPL-2.1-or-later
# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2022-2024 The FreeCAD Project Association AISBL         *
# *                                                                         *
# *   This file is part of FreeCAD.                                         *
# *                                                                         *
# *   FreeCAD is free software: you can redistribute it and/or modify it    *
# *   under the terms of the GNU Lesser General Public License as           *
# *   published by the Free Software Foundation, either version 2.1 of the  *
# *   License, or (at your option) any later version.                       *
# *                                                                         *
# *   FreeCAD is distributed in the hope that it will be useful, but        *
# *   WITHOUT ANY WARRANTY; without even the implied warranty of            *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU      *
# *   Lesser General Public License for more details.                       *
# *                                                                         *
# *   You should have received a copy of the GNU Lesser General Public      *
# *   License along with FreeCAD. If not, see                               *
# *   <https://www.gnu.org/licenses/>.                                      *
# *                                                                         *
# ***************************************************************************
import re

import addonmanager_freecad_interface as fci

from PySideWrapper import QtCore, QtGui, QtWidgets

from typing import Optional


class WidgetReadmeBrowser(QtWidgets.QTextBrowser):
    """A QTextBrowser widget that emits signals for each requested image resource, allowing an external controller
    to load and re-deliver those images. Once all resources have been re-delivered, the original data is redisplayed
    with the images in-line. Call setUrl prior to calling setMarkdown or setHtml to ensure URLs are resolved
    correctly."""

    load_resource = QtCore.Signal(str)  # Str is a URL to a resource
    follow_link = QtCore.Signal(str)  # Str is a URL to another page

    def __init__(self, parent: QtWidgets.QWidget = None):
        super().__init__(parent)
        self.image_map = {}
        self.url = ""
        self.stop = False
        self.setOpenExternalLinks(True)

    def setUrl(self, url: str):
        """Set the base URL of the page. Used to resolve relative URLs in the page source."""
        self.url = url

    def setMarkdown(self, md: str):
        """Provides an optional fallback to the markdown library for older versions of Qt (prior to 5.15) that did not
        have native markdown support. Lacking that, plaintext is displayed."""
        geometry = self.geometry()
        if hasattr(super(), "setMarkdown"):

            super().setMarkdown(self._clean_markdown(md))
        else:
            try:
                import markdown

                html = markdown.markdown(md)
                self.setHtml(html)
            except ImportError:
                self.setText(md)
                fci.Console.Warning(
                    "Qt < 5.15 and no `import markdown` -- falling back to plain text display\n"
                )
        self.setGeometry(geometry)

    def _clean_markdown(self, md: str):
        # Remove some HTML tags (for now just img and br, which are the most common offenders that break rendering)
        br_re = re.compile(r"<br\s*/?>", re.IGNORECASE)
        comment_re = re.compile(r"<!--.*?-->", re.DOTALL)
        img_re = re.compile(
            r'<img\s+[^>]*src=["\'](?P<src>[^"\']+)["\'][^>]*'
            r'(alt=["\'](?P<alt>[^"\']*)["\'])?[^>]*/?>',
            re.IGNORECASE,
        )

        # Replace html images to markdown
        def _markdown_img(m):
            src = m.group("src")
            alt = m.group("alt") or ""
            return f"![{alt}]({src})"

        cleaned = br_re.sub(r"\n", md)
        cleaned = comment_re.sub("", cleaned)
        cleaned = img_re.sub(_markdown_img, cleaned)

        return cleaned

    def set_resource(self, resource_url: str, image: Optional[QtGui.QImage]):
        """Once a resource has been fetched (or the fetch has failed), this method should be used to inform the widget
        that the resource has been loaded. Note that the incoming image is scaled to 97% of the widget width if it is
        larger than that."""
        self.image_map[resource_url] = self._ensure_appropriate_width(image)

    def loadResource(self, resource_type: int, name: QtCore.QUrl) -> object:
        """Callback for resource loading. Called automatically by underlying Qt
        code when external resources are needed for rendering. In particular,
        here it is used to download and cache (in RAM) the images needed for the
        README and Wiki pages."""
        if resource_type == QtGui.QTextDocument.ImageResource and not self.stop:
            full_url = self._create_full_url(name.toString())
            if full_url not in self.image_map:
                self.load_resource.emit(full_url)
                self.image_map[full_url] = None
            return self.image_map[full_url]
        elif resource_type == QtGui.QTextDocument.MarkdownResource:
            self.follow_link.emit(name.toString())
            return self.toMarkdown()
        elif resource_type == QtGui.QTextDocument.HtmlResource:
            self.follow_link.emit(name.toString())
            return self.toHtml()
        return super().loadResource(resource_type, name)

    def _ensure_appropriate_width(self, image: QtGui.QImage) -> QtGui.QImage:
        ninety_seven_percent = self.width() * 0.97
        if image.width() < ninety_seven_percent:
            return image
        return image.scaledToWidth(ninety_seven_percent)

    def _create_full_url(self, url: str) -> str:
        if url.startswith("http"):
            return url
        if not self.url:
            return url
        lhs, slash, _ = self.url.rpartition("/")
        return lhs + slash + url
