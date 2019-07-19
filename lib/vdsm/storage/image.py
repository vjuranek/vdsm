#
# Copyright 2009-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from __future__ import absolute_import

import os
import logging
import threading
import uuid
from contextlib import contextmanager

from vdsm import constants
from vdsm import utils
from vdsm import virtsparsify
from vdsm.config import config
from vdsm.common import cmdutils
from vdsm.common import logutils
from vdsm.common.threadlocal import vars
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import imageSharing
from vdsm.storage import misc
from vdsm.storage import qemuimg
from vdsm.storage import resourceManager as rm
from vdsm.storage import sd
from vdsm.storage import volume
from vdsm.storage import workarounds
from vdsm.storage.sdc import sdCache

from vdsm.common.exception import ActionStopped

log = logging.getLogger('storage.Image')

# What volumes to synchronize
SYNC_VOLUMES_ALL = 'ALL'
SYNC_VOLUMES_INTERNAL = 'INTERNAL'
SYNC_VOLUMES_LEAF = 'LEAF'

# Image Operations
UNKNOWN_OP = 0
COPY_OP = 1
MOVE_OP = 2
OP_TYPES = {UNKNOWN_OP: 'UNKNOWN', COPY_OP: 'COPY', MOVE_OP: 'MOVE'}

RENAME_RANDOM_STRING_LEN = 8


def _deleteImage(dom, imgUUID, postZero, discard):
    """This ancillary function will be removed.

    Replaces Image.delete() in Image.[copyCollapsed(), move(), multimove()].
    """
    allVols = dom.getAllVolumes()
    imgVols = sd.getVolsOfImage(allVols, imgUUID)
    if not imgVols:
        log.warning("No volumes found for image %s. %s", imgUUID, allVols)
        return
    elif postZero:
        dom.zeroImage(dom.sdUUID, imgUUID, imgVols, discard)
    else:
        dom.deleteImage(dom.sdUUID, imgUUID, imgVols)


class Image:
    """ Actually represents a whole virtual disk.
        Consist from chain of volumes.
    """
    log = logging.getLogger('storage.Image')
    _fakeTemplateLock = threading.Lock()

    def __init__(self, repoPath):
        self._repoPath = repoPath

    @property
    def repoPath(self):
        return self._repoPath

    def _run_qemuimg_operation(self, operation):
        self.log.debug('running qemu-img operation')
        with vars.task.abort_callback(operation.abort):
            operation.run()
        self.log.debug('qemu-img operation has completed')

    def deletedVolumeName(self, uuid):
        """
        Create REMOVED_IMAGE_PREFIX + <random> + uuid string.
        """
        randomStr = misc.randomStr(RENAME_RANDOM_STRING_LEN)
        return "%s%s_%s" % (sc.REMOVED_IMAGE_PREFIX, randomStr, uuid)

    def estimate_qcow2_size_blk(self, src_vol_params, dst_sd_id):
        """
        Calculate volume allocation size for converting raw/qcow2
        source volume to qcow2 volume on destination storage domain.

        Arguments:
            src_vol_params(dict): Dictionary returned from
                                  `storage.volume.Volume.getVolumeParams()`
            dst_sd_id(str) : Destination volume storage domain id

        Returns:
            Volume allocation in blocks
        """
        # measure required size.
        qemu_measure = qemuimg.measure(
            image=src_vol_params['path'],
            format=sc.fmt2str(src_vol_params['volFormat']),
            output_format=qemuimg.FORMAT.QCOW2)

        # Adds extra room so we don't have to extend this disk immediately
        # when a vm is started.
        chunk_size_mb = config.getint("irs", "volume_utilization_chunk_mb")
        chunk_size = chunk_size_mb * constants.MEGAB
        size_blk = (qemu_measure["required"] + chunk_size) // sc.BLOCK_SIZE
        # Limit estimates size by maximum size.
        vol_class = sdCache.produce(dst_sd_id).getVolumeClass()
        max_size = vol_class.max_size(src_vol_params['capacity'],
                                      sc.COW_FORMAT)
        size_blk = min(size_blk, max_size // sc.BLOCK_SIZE)

        # Return estimated size of allocation blocks.
        self.log.debug("Estimated allocation for qcow2 volume:"
                       "%d blocks", size_blk)
        return size_blk

    def estimateChainSizeBlk(self, sdUUID, imgUUID, volUUID, size):
        """
        Compute an estimate of the whole chain size
        using the sum of the actual size of the chain's volumes
        """
        chain = self.getChain(sdUUID, imgUUID, volUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in chain)
        self.log.info("chain=%s ", log_str)

        new_size_blk = 0
        template = chain[0].getParentVolume()
        if template:
            new_size_blk = template.getVolumeSize(bs=1) // sc.BLOCK_SIZE_512
        for vol in chain:
            new_size_blk += vol.getVolumeSize(bs=1) // sc.BLOCK_SIZE_512
        if new_size_blk > size:
            new_size_blk = size
        # allocate %10 more for cow metadata
        new_size_blk = int(new_size_blk * sc.COW_OVERHEAD)
        return new_size_blk

    def getChain(self, sdUUID, imgUUID, volUUID=None):
        """
        Return the chain of volumes of image as a sorted list
        (not including a shared base (template) if any)
        """
        chain = []
        volclass = sdCache.produce(sdUUID).getVolumeClass()

        # Use volUUID when provided
        if volUUID:
            srcVol = volclass(self.repoPath, sdUUID, imgUUID, volUUID)

            # For template images include only one volume (the template itself)
            # NOTE: this relies on the fact that in a template there is only
            #       one volume
            if srcVol.isShared():
                return [srcVol]

        # Find all the volumes when volUUID is not provided
        else:
            # Find all volumes of image
            uuidlist = volclass.getImageVolumes(sdUUID, imgUUID)

            if not uuidlist:
                raise se.ImageDoesNotExistInSD(imgUUID, sdUUID)

            srcVol = volclass(self.repoPath, sdUUID, imgUUID, uuidlist[0])

            # For template images include only one volume (the template itself)
            if len(uuidlist) == 1 and srcVol.isShared():
                return [srcVol]

            # Searching for the leaf
            for vol in uuidlist:
                srcVol = volclass(self.repoPath, sdUUID, imgUUID, vol)

                if srcVol.isLeaf():
                    break

                srcVol = None

            if not srcVol:
                self.log.error("There is no leaf in the image %s", imgUUID)
                raise se.ImageIsNotLegalChain(imgUUID)

        # We have seen corrupted chains that cause endless loops here.
        # https://bugzilla.redhat.com/1125197
        seen = set()

        # Build up the sorted parent -> child chain
        while not srcVol.isShared():
            chain.insert(0, srcVol)
            seen.add(srcVol.volUUID)

            parentUUID = srcVol.getParent()
            if parentUUID == sc.BLANK_UUID:
                break

            if parentUUID in seen:
                self.log.error("Image %s volume %s has invalid parent UUID %s",
                               imgUUID, srcVol.volUUID, parentUUID)
                raise se.ImageIsNotLegalChain(imgUUID)

            srcVol = srcVol.getParentVolume()

        return chain

    def getTemplate(self, sdUUID, imgUUID):
        """
        Return template of the image
        """
        tmpl = None
        # Find all volumes of image (excluding template)
        chain = self.getChain(sdUUID, imgUUID)
        # check if the chain is build above a template, or it is a standalone
        pvol = chain[0].getParentVolume()
        if pvol:
            tmpl = pvol
        elif chain[0].isShared():
            tmpl = chain[0]

        return tmpl

    def createFakeTemplate(self, sdUUID, volParams):
        """
        Create fake template (relevant for Backup domain only)
        """
        with self._fakeTemplateLock:
            try:
                destDom = sdCache.produce(sdUUID)
                volclass = destDom.getVolumeClass()
                # Validate that the destination template exists and accessible
                volclass(self.repoPath, sdUUID, volParams['imgUUID'],
                         volParams['volUUID'])
            except (se.VolumeDoesNotExist, se.ImagePathError):
                try:
                    # Create fake parent volume
                    destDom.createVolume(
                        imgUUID=volParams['imgUUID'],
                        size_blk=volParams['capacity'] // sc.BLOCK_SIZE_512,
                        volFormat=sc.COW_FORMAT,
                        preallocate=sc.SPARSE_VOL,
                        diskType=volParams['disktype'],
                        volUUID=volParams['volUUID'], desc="Fake volume",
                        srcImgUUID=sc.BLANK_UUID,
                        srcVolUUID=sc.BLANK_UUID)

                    vol = destDom.produceVolume(imgUUID=volParams['imgUUID'],
                                                volUUID=volParams['volUUID'])
                    # Mark fake volume as "FAKE"
                    vol.setLegality(sc.FAKE_VOL)
                    # Mark fake volume as shared
                    vol.setShared()
                    # Now we should re-link all hardlinks of this template in
                    # all VMs based on it
                    destDom.templateRelink(volParams['imgUUID'],
                                           volParams['volUUID'])

                    self.log.debug("Succeeded to create fake image %s in "
                                   "domain %s", volParams['imgUUID'],
                                   destDom.sdUUID)
                except Exception:
                    self.log.error("Failure to create fake image %s in domain "
                                   "%s", volParams['imgUUID'], destDom.sdUUID,
                                   exc_info=True)

    def isLegal(self, sdUUID, imgUUID):
        """
        Check correctness of the whole chain (excluding template)
        """
        try:
            legal = True
            volclass = sdCache.produce(sdUUID).getVolumeClass()
            vollist = volclass.getImageVolumes(sdUUID, imgUUID)
            self.log.info("image %s in domain %s has vollist %s", imgUUID,
                          sdUUID, str(vollist))
            for v in vollist:
                vol = volclass(self.repoPath, sdUUID, imgUUID, v)
                if not vol.isLegal() or vol.isFake():
                    legal = False
                    break
        except:
            legal = False
        return legal

    def __cleanupMove(self, srcVol, dstVol):
        """
        Cleanup environments after move operation
        """
        try:
            if srcVol:
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)
            if dstVol:
                dstVol.teardown(sdUUID=dstVol.sdUUID, volUUID=dstVol.volUUID)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)

    def _createTargetImage(self, destDom, srcSdUUID, imgUUID):
        # Before actual data copying we need perform several operation
        # such as: create all volumes, create fake template if needed, ...
        try:
            # Find all volumes of source image
            srcChain = self.getChain(srcSdUUID, imgUUID)
            log_str = logutils.volume_chain_to_str(
                vol.volUUID for vol in srcChain)
            self.log.info("Source chain=%s ", log_str)
        except se.StorageException:
            self.log.error("Unexpected error", exc_info=True)
            raise
        except Exception as e:
            self.log.error("Unexpected error", exc_info=True)
            raise se.SourceImageActionError(imgUUID, srcSdUUID, str(e))

        fakeTemplate = False
        pimg = sc.BLANK_UUID    # standalone chain
        # check if the chain is build above a template, or it is a standalone
        pvol = srcChain[0].getParentVolume()
        if pvol:
            # find out parent volume parameters
            volParams = pvol.getVolumeParams()
            pimg = volParams['imgUUID']      # pimg == template image
            if destDom.isBackup():
                # FIXME: This workaround help as copy VM to the backup domain
                #        without its template. We will create fake template
                #        for future VM creation and mark it as FAKE volume.
                #        This situation is relevant for backup domain only.
                fakeTemplate = True

        @contextmanager
        def justLogIt(img):
            self.log.debug("You don't really need lock parent of image %s",
                           img)
            yield

        dstImageResourcesNamespace = rm.getNamespace(sc.IMAGE_NAMESPACE,
                                                     destDom.sdUUID)
        # In destination domain we need to lock image's template if exists
        with rm.acquireResource(dstImageResourcesNamespace, pimg, rm.SHARED) \
                if pimg != sc.BLANK_UUID else justLogIt(imgUUID):
            if fakeTemplate:
                self.createFakeTemplate(destDom.sdUUID, volParams)

            dstChain = []
            for srcVol in srcChain:
                # Create the dst volume
                try:
                    # find out src volume parameters
                    volParams = srcVol.getVolumeParams()

                    # To avoid prezeroing preallocated volumes on NFS domains
                    # we create the target as a sparse volume (since it will be
                    # soon filled with the data coming from the copy) and then
                    # we change its metadata back to the original value.
                    if (destDom.supportsSparseness or
                            volParams['volFormat'] != sc.RAW_FORMAT):
                        tmpVolPreallocation = sc.SPARSE_VOL
                    else:
                        tmpVolPreallocation = sc.PREALLOCATED_VOL

                    destDom.createVolume(
                        imgUUID=imgUUID,
                        size_blk=volParams['capacity'] // sc.BLOCK_SIZE_512,
                        volFormat=volParams['volFormat'],
                        preallocate=tmpVolPreallocation,
                        diskType=volParams['disktype'],
                        volUUID=srcVol.volUUID,
                        desc=volParams['descr'],
                        srcImgUUID=pimg,
                        srcVolUUID=volParams['parent'])

                    dstVol = destDom.produceVolume(imgUUID=imgUUID,
                                                   volUUID=srcVol.volUUID)

                    # Extend volume (for LV only) size to the actual size
                    size_aligned = utils.round(
                        volParams['apparentsize'], sc.BLOCK_SIZE_512)
                    dstVol.extend(size_aligned // sc.BLOCK_SIZE_512)

                    # Change destination volume metadata to preallocated in
                    # case we've used a sparse volume to accelerate the
                    # volume creation
                    if volParams['prealloc'] == sc.PREALLOCATED_VOL \
                            and tmpVolPreallocation != sc.PREALLOCATED_VOL:
                        dstVol.setType(sc.PREALLOCATED_VOL)

                    dstChain.append(dstVol)
                except se.StorageException:
                    self.log.error("Unexpected error", exc_info=True)
                    raise
                except Exception as e:
                    self.log.error("Unexpected error", exc_info=True)
                    raise se.DestImageActionError(imgUUID, destDom.sdUUID,
                                                  str(e))

                # only base may have a different parent image
                pimg = imgUUID

        return {'srcChain': srcChain, 'dstChain': dstChain}

    def _interImagesCopy(self, destDom, srcSdUUID, imgUUID, chains):
        srcLeafVol = chains['srcChain'][-1]
        dstLeafVol = chains['dstChain'][-1]
        try:
            # Prepare the whole chains before the copy
            srcLeafVol.prepare(rw=False)
            dstLeafVol.prepare(rw=True, chainrw=True, setrw=True)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)
            # teardown volumes
            self.__cleanupMove(srcLeafVol, dstLeafVol)
            raise

        try:
            for srcVol in chains['srcChain']:
                # Do the actual copy
                try:
                    dstVol = destDom.produceVolume(imgUUID=imgUUID,
                                                   volUUID=srcVol.volUUID)

                    if workarounds.invalid_vm_conf_disk(srcVol):
                        srcFormat = dstFormat = qemuimg.FORMAT.RAW
                    else:
                        srcFormat = sc.fmt2str(srcVol.getFormat())
                        dstFormat = sc.fmt2str(dstVol.getFormat())

                    parentVol = dstVol.getParentVolume()

                    if parentVol is not None:
                        backing = volume.getBackingVolumePath(
                            imgUUID, parentVol.volUUID)
                        backingFormat = sc.fmt2str(parentVol.getFormat())
                    else:
                        backing = None
                        backingFormat = None

                    if (destDom.supportsSparseness and
                            dstVol.getType() == sc.PREALLOCATED_VOL):
                        preallocation = qemuimg.PREALLOCATION.FALLOC
                    else:
                        preallocation = None

                    operation = qemuimg.convert(
                        srcVol.getVolumePath(),
                        dstVol.getVolumePath(),
                        srcFormat=srcFormat,
                        dstFormat=dstFormat,
                        dstQcow2Compat=destDom.qcow2_compat(),
                        backing=backing,
                        backingFormat=backingFormat,
                        preallocation=preallocation,
                        unordered_writes=destDom.recommends_unordered_writes(
                            dstVol.getFormat()))
                    with utils.stopwatch("Copy volume %s"
                                         % srcVol.volUUID):
                        self._run_qemuimg_operation(operation)
                except ActionStopped:
                    raise
                except se.StorageException:
                    self.log.error("Unexpected error", exc_info=True)
                    raise
                except Exception:
                    self.log.error("Copy image error: image=%s, src domain=%s,"
                                   " dst domain=%s", imgUUID, srcSdUUID,
                                   destDom.sdUUID, exc_info=True)
                    raise se.CopyImageError()
        finally:
            # teardown volumes
            self.__cleanupMove(srcLeafVol, dstLeafVol)

    def _finalizeDestinationImage(self, destDom, imgUUID, chains, force):
        for srcVol in chains['srcChain']:
            try:
                dstVol = destDom.produceVolume(imgUUID=imgUUID,
                                               volUUID=srcVol.volUUID)
                # In case of copying template, we should set the destination
                # volume as SHARED (after copy because otherwise prepare as RW
                # would fail)
                if srcVol.isShared():
                    dstVol.setShared()
                elif srcVol.isInternal():
                    dstVol.setInternal()
            except se.StorageException:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception as e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.DestImageActionError(imgUUID, destDom.sdUUID, str(e))

    def move(self, srcSdUUID, dstSdUUID, imgUUID, vmUUID, op, postZero, force,
             discard):
        """
        Move/Copy image between storage domains within same storage pool
        """
        self.log.info("srcSdUUID=%s dstSdUUID=%s imgUUID=%s vmUUID=%s op=%s "
                      "force=%s postZero=%s discard=%s", srcSdUUID, dstSdUUID,
                      imgUUID, vmUUID, OP_TYPES[op], str(force), str(postZero),
                      discard)

        destDom = sdCache.produce(dstSdUUID)
        # If image already exists check whether it illegal/fake, overwrite it
        if not self.isLegal(destDom.sdUUID, imgUUID):
            force = True
        # We must first remove the previous instance of image (if exists)
        # in destination domain, if we got the overwrite command
        if force:
            self.log.info("delete image %s on domain %s before overwriting",
                          imgUUID, destDom.sdUUID)
            _deleteImage(destDom, imgUUID, postZero, discard)

        chains = self._createTargetImage(destDom, srcSdUUID, imgUUID)
        self._interImagesCopy(destDom, srcSdUUID, imgUUID, chains)
        self._finalizeDestinationImage(destDom, imgUUID, chains, force)
        if force:
            leafVol = chains['dstChain'][-1]
            # Now we should re-link all deleted hardlinks, if exists
            destDom.templateRelink(imgUUID, leafVol.volUUID)

        # At this point we successfully finished the 'copy' part of the
        # operation and we can clear all recoveries.
        vars.task.clearRecoveries()
        # If it's 'move' operation, we should delete src image after copying
        if op == MOVE_OP:
            # TODO: Should raise here.
            try:
                dom = sdCache.produce(srcSdUUID)
                _deleteImage(dom, imgUUID, postZero, discard)
            except se.StorageException:
                self.log.warning("Failed to remove img: %s from srcDom %s: "
                                 "after it was copied to: %s", imgUUID,
                                 srcSdUUID, dstSdUUID)

        self.log.info("%s task on image %s was successfully finished",
                      OP_TYPES[op], imgUUID)
        return True

    def _getSparsifyVolume(self, sdUUID, imgUUID, volUUID):
        # FIXME:
        # sdCache.produce.produceVolume gives volumes that return getVolumePath
        # with a colon (:) for NFS servers. So, we're using volClass().
        # https://bugzilla.redhat.com/1128942
        # If, and when the bug gets solved, use
        # sdCache.produce(...).produceVolume(...) to create the volumes.
        volClass = sdCache.produce(sdUUID).getVolumeClass()
        return volClass(self.repoPath, sdUUID, imgUUID, volUUID)

    def sparsify(self, tmpSdUUID, tmpImgUUID, tmpVolUUID, dstSdUUID,
                 dstImgUUID, dstVolUUID):
        """
        Reduce sparse image size by converting free space on image to free
        space on storage domain using virt-sparsify.
        """
        self.log.info("tmpSdUUID=%s, tmpImgUUID=%s, tmpVolUUID=%s, "
                      "dstSdUUID=%s, dstImgUUID=%s, dstVolUUID=%s", tmpSdUUID,
                      tmpImgUUID, tmpVolUUID, dstSdUUID, dstImgUUID,
                      dstVolUUID)

        tmpVolume = self._getSparsifyVolume(tmpSdUUID, tmpImgUUID, tmpVolUUID)
        dstVolume = self._getSparsifyVolume(dstSdUUID, dstImgUUID, dstVolUUID)

        if not dstVolume.isSparse():
            raise se.VolumeNotSparse()

        srcVolume = self._getSparsifyVolume(tmpSdUUID, tmpImgUUID,
                                            tmpVolume.getParent())

        tmpVolume.prepare()
        try:
            dstVolume.prepare()
            try:
                # By definition "sparsification" is implemented writing a file
                # with zeroes as large as the entire file-system. So at least
                # tmpVolume needs to be as large as the virtual disk size for
                # the worst case.
                # TODO: Some extra space may be needed for QCOW2 headers
                tmpVolume.extend(tmpVolume.getCapacity() // sc.BLOCK_SIZE_512)
                # For the dstVolume we may think of an optimization where the
                # extension is as large as the source (and at the end we
                # shrinkToOptimalSize).
                # TODO: Extend the dstVolume only as much as the actual size of
                # srcVolume
                # TODO: Some extra space may be needed for QCOW2 headers
                dstVolume.extend(tmpVolume.getCapacity() // sc.BLOCK_SIZE_512)

                srcFormat = sc.fmt2str(srcVolume.getFormat())
                dstFormat = sc.fmt2str(dstVolume.getFormat())

                virtsparsify.sparsify(srcVolume.getVolumePath(),
                                      tmpVolume.getVolumePath(),
                                      dstVolume.getVolumePath(),
                                      src_format=srcFormat,
                                      dst_format=dstFormat)
            except Exception:
                self.log.exception('Unexpected error sparsifying %s',
                                   tmpVolUUID)
                raise se.CannotSparsifyVolume(tmpVolUUID)
            finally:
                dstVolume.teardown(sdUUID=dstSdUUID, volUUID=dstVolUUID)
        finally:
            tmpVolume.teardown(sdUUID=tmpSdUUID, volUUID=tmpVolUUID)

        self._shrinkVolumeToOptimalSize(tmpVolume)
        self._shrinkVolumeToOptimalSize(dstVolume)

    def cloneStructure(self, sdUUID, imgUUID, dstSdUUID):
        self._createTargetImage(sdCache.produce(dstSdUUID), sdUUID, imgUUID)

    def syncData(self, sdUUID, imgUUID, dstSdUUID, syncType):
        srcChain = self.getChain(sdUUID, imgUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in srcChain)
        self.log.info("Source chain=%s ", log_str)

        dstChain = self.getChain(dstSdUUID, imgUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in dstChain)
        self.log.info("Dest chain=%s ", log_str)

        if syncType == SYNC_VOLUMES_INTERNAL:
            try:
                # Removing the leaf volumes
                del srcChain[-1], dstChain[-1]
            except IndexError:
                raise se.ImageIsNotLegalChain()
        elif syncType == SYNC_VOLUMES_LEAF:
            try:
                # Removing all the internal volumes
                del srcChain[:-1], dstChain[:-1]
            except IndexError:
                raise se.ImageIsNotLegalChain()
        elif syncType != SYNC_VOLUMES_ALL:
            raise se.MiscNotImplementedException()

        if len(srcChain) != len(dstChain):
            raise se.DestImageActionError(imgUUID, dstSdUUID)

        # Checking the volume uuids (after removing the leaves to allow
        # different uuids for the current top layer, see previous check).
        for i, v in enumerate(srcChain):
            if v.volUUID != dstChain[i].volUUID:
                raise se.DestImageActionError(imgUUID, dstSdUUID)

        dstDom = sdCache.produce(dstSdUUID)

        self._interImagesCopy(dstDom, sdUUID, imgUUID,
                              {'srcChain': srcChain, 'dstChain': dstChain})
        self._finalizeDestinationImage(dstDom, imgUUID,
                                       {'srcChain': srcChain,
                                        'dstChain': dstChain}, False)

    def __cleanupCopy(self, srcVol, dstVol):
        """
        Cleanup environments after copy operation
        """
        try:
            if srcVol:
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)
            if dstVol:
                dstVol.teardown(sdUUID=dstVol.sdUUID, volUUID=dstVol.volUUID)
        except Exception:
            self.log.error("Unexpected error", exc_info=True)

    def validateVolumeChain(self, sdUUID, imgUUID):
        """
        Check correctness of the whole chain (including template if exists)
        """
        if not self.isLegal(sdUUID, imgUUID):
            raise se.ImageIsNotLegalChain(imgUUID)
        chain = self.getChain(sdUUID, imgUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in chain)
        self.log.info("Current chain=%s ", log_str)

        # check if the chain is build above a template, or it is a standalone
        pvol = chain[0].getParentVolume()
        if pvol:
            if not pvol.isLegal() or pvol.isFake():
                raise se.ImageIsNotLegalChain(imgUUID)

    def copyCollapsed(self, sdUUID, vmUUID, srcImgUUID, srcVolUUID, dstImgUUID,
                      dstVolUUID, descr, dstSdUUID, volType, volFormat,
                      preallocate, postZero, force, discard):
        """
        Create new template/volume from VM.
        Do it by collapse and copy the whole chain (baseVolUUID->srcVolUUID)
        """
        self.log.info("sdUUID=%s vmUUID=%s srcImgUUID=%s srcVolUUID=%s "
                      "dstImgUUID=%s dstVolUUID=%s dstSdUUID=%s volType=%s "
                      "volFormat=%s preallocate=%s force=%s postZero=%s "
                      "discard=%s",
                      sdUUID, vmUUID, srcImgUUID, srcVolUUID, dstImgUUID,
                      dstVolUUID, dstSdUUID, volType,
                      sc.type2name(volFormat), sc.type2name(preallocate),
                      str(force), str(postZero), discard)
        try:
            srcVol = dstVol = None

            # Find out dest sdUUID
            if dstSdUUID == sd.BLANK_UUID:
                dstSdUUID = sdUUID
            volclass = sdCache.produce(sdUUID).getVolumeClass()
            destDom = sdCache.produce(dstSdUUID)

            # find src volume
            try:
                srcVol = volclass(self.repoPath, sdUUID, srcImgUUID,
                                  srcVolUUID)
            except se.StorageException:
                raise
            except Exception as e:
                self.log.error(e, exc_info=True)
                raise se.SourceImageActionError(srcImgUUID, sdUUID, str(e))

            # Create dst volume
            try:
                # Before reading source volume parameters from volume metadata,
                # prepare the volume. This ensure that the volume capacity will
                # match the actual virtual size, see
                # https://bugzilla.redhat.com/1700623.
                srcVol.prepare(rw=False)

                volParams = srcVol.getVolumeParams()

                if volFormat in [sc.COW_FORMAT, sc.RAW_FORMAT]:
                    dstVolFormat = volFormat
                else:
                    dstVolFormat = volParams['volFormat']

                # TODO: This is needed only when copying to qcow2-thin volume
                # on block storage. Move into calculate_initial_size_blk.
                dstVolAllocBlk = self.calculate_vol_alloc_blk(
                    sdUUID, volParams, dstSdUUID, dstVolFormat)

                # Find out dest volume parameters
                if preallocate in [sc.PREALLOCATED_VOL, sc.SPARSE_VOL]:
                    volParams['prealloc'] = preallocate

                initialSizeBlk = self.calculate_initial_size_blk(
                    destDom.supportsSparseness,
                    dstVolFormat,
                    volParams['prealloc'],
                    dstVolAllocBlk)

                self.log.info(
                    "Copy source %s:%s:%s to destination %s:%s:%s size=%s "
                    "blocks, initial size=%s blocks",
                    sdUUID,
                    srcImgUUID,
                    srcVolUUID,
                    dstSdUUID,
                    dstImgUUID,
                    dstVolUUID,
                    volParams['capacity'] // sc.BLOCK_SIZE_512,
                    initialSizeBlk)

                # If image already exists check whether it illegal/fake,
                # overwrite it
                if not self.isLegal(dstSdUUID, dstImgUUID):
                    force = True

                # We must first remove the previous instance of image (if
                # exists) in destination domain, if we got the overwrite
                # command
                if force:
                    self.log.info("delete image %s on domain %s before "
                                  "overwriting", dstImgUUID, dstSdUUID)
                    _deleteImage(destDom, dstImgUUID, postZero, discard)

                destDom.createVolume(
                    imgUUID=dstImgUUID,
                    size_blk=volParams['capacity'] // sc.BLOCK_SIZE_512,
                    volFormat=dstVolFormat,
                    preallocate=volParams['prealloc'],
                    diskType=volParams['disktype'],
                    volUUID=dstVolUUID,
                    desc=descr,
                    srcImgUUID=sc.BLANK_UUID,
                    srcVolUUID=sc.BLANK_UUID,
                    initial_size_blk=initialSizeBlk)

                dstVol = sdCache.produce(dstSdUUID).produceVolume(
                    imgUUID=dstImgUUID, volUUID=dstVolUUID)

            except se.StorageException:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception as e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.CopyImageError("Destination volume %s error: %s" %
                                        (dstVolUUID, str(e)))

            try:
                # Start the actual copy image procedure
                dstVol.prepare(rw=True, setrw=True)

                if (destDom.supportsSparseness and
                        dstVol.getType() == sc.PREALLOCATED_VOL):
                    preallocation = qemuimg.PREALLOCATION.FALLOC
                else:
                    preallocation = None

                try:
                    operation = qemuimg.convert(
                        volParams['path'],
                        dstVol.getVolumePath(),
                        srcFormat=sc.fmt2str(volParams['volFormat']),
                        dstFormat=sc.fmt2str(dstVolFormat),
                        dstQcow2Compat=destDom.qcow2_compat(),
                        preallocation=preallocation,
                        unordered_writes=destDom.recommends_unordered_writes(
                            dstVolFormat))
                    with utils.stopwatch("Copy volume %s"
                                         % srcVol.volUUID):
                        self._run_qemuimg_operation(operation)
                except ActionStopped:
                    raise
                except cmdutils.Error as e:
                    self.log.exception('conversion failure for volume %s',
                                       srcVol.volUUID)
                    raise se.CopyImageError(str(e))

                # Mark volume as SHARED
                if volType == sc.SHARED_VOL:
                    dstVol.setShared()

                dstVol.setLegality(sc.LEGAL_VOL)

                if force:
                    # Now we should re-link all deleted hardlinks, if exists
                    destDom.templateRelink(dstImgUUID, dstVolUUID)
            except se.StorageException:
                self.log.error("Unexpected error", exc_info=True)
                raise
            except Exception as e:
                self.log.error("Unexpected error", exc_info=True)
                raise se.CopyImageError("src image=%s, dst image=%s: msg=%s" %
                                        (srcImgUUID, dstImgUUID, str(e)))

            self.log.info("Finished copying %s:%s -> %s:%s", sdUUID,
                          srcVolUUID, dstSdUUID, dstVolUUID)
            # TODO: handle return status
            return dstVolUUID
        finally:
            self.__cleanupCopy(srcVol=srcVol, dstVol=dstVol)

    def calculate_initial_size_blk(self, is_file, format, prealloc,
                                   estimate_blk):
        """
        Return the initial size for creating a volume during copyCollapsed.

        Arguments:
            is_file (bool): destination storage domain is file domain.
            format (int): destination volume format enum.
            prealloc (int): destination volume preallocation enum.
            estimate_blk (int): estimated allocation in 512 blocks.
        """
        if is_file:
            # Avoid slow preallocation of raw-preallocated volumes on file
            # based storage.
            if format == sc.RAW_FORMAT and prealloc == sc.PREALLOCATED_VOL:
                return 0
        else:
            # Ensure that enough extents are allocated for raw-preallocated
            # volume on block storage.
            # TODO: Calculate the value here.
            if format == sc.COW_FORMAT and prealloc == sc.SPARSE_VOL:
                return estimate_blk

        # Otherwise no initial size is used.
        return None

    def calculate_vol_alloc_blk(self, src_sd_id, src_vol_params,
                                dst_sd_id, dst_vol_format):
        """
        Calculate destination volume allocation size for copying source volume.

        Arguments:
            src_sd_id (str): Source volume storage domain id
            src_vol_params (dict): Dictionary returned from
                                   `storage.volume.Volume.getVolumeParams()`
            dst_sd_id (str): Destination volume storage domain id
            dst_vol_format (int): One of sc.RAW_FORMAT, sc.COW_FORMAT

        Returns:
            Volume allocation in blocks
        """
        if dst_vol_format == sc.RAW_FORMAT:
            # destination 'raw'.
            # The actual volume size must be the src virtual size.
            return src_vol_params['capacity'] // sc.BLOCK_SIZE_512
        else:
            # destination 'cow'.
            # The actual volume size can be more than virtual size
            # due to qcow2 metadata.
            if src_vol_params['volFormat'] == sc.COW_FORMAT:
                # source 'cow'
                if src_vol_params['parent'] != sc.BLANK_UUID:
                    # source 'cow' with parent
                    # Using estimated size of the chain.
                    if src_vol_params['prealloc'] != sc.SPARSE_VOL:
                        raise se.IncorrectFormat(self)
                    return self.estimateChainSizeBlk(
                        src_sd_id,
                        src_vol_params['imgUUID'],
                        src_vol_params['volUUID'],
                        src_vol_params['capacity'] // sc.BLOCK_SIZE_512)
                else:
                    # source 'cow' without parent.
                    # Use estimate for supporting compressed source images, for
                    # example, uploaded compressed qcow2 appliance.
                    return self.estimate_qcow2_size_blk(
                        src_vol_params, dst_sd_id)
            else:
                # source 'raw'.
                # Add additional space for qcow2 metadata.
                return self.estimate_qcow2_size_blk(src_vol_params, dst_sd_id)

    def markIllegalSubChain(self, sdDom, imgUUID, chain):
        """
        Mark all volumes in the sub-chain as illegal
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        volclass = sdDom.getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        tmpVol = volclass(self.repoPath, sdDom.sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdDom.sdUUID, imgUUID,
                             ancestor).getParent()

        # Mark all volumes as illegal
        while tmpVol and dstParent != tmpVol.volUUID:
            vol = tmpVol.getParentVolume()
            tmpVol.setLegality(sc.ILLEGAL_VOL)
            tmpVol = vol

    def __teardownSubChain(self, sdUUID, imgUUID, chain):
        """
        Teardown all volumes in the sub-chain
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        # Teardown subchain ('ancestor' ->...-> 'successor') volumes
        # before they will deleted.
        # This subchain include volumes that were merged (rebased)
        # into 'successor' and now should be deleted.
        # We prepared all these volumes as part of preparing the whole
        # chain before rebase, but during rebase we detached all of them from
        # the chain and couldn't teardown they properly.
        # So, now we must teardown them to release they resources.
        volclass = sdCache.produce(sdUUID).getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        srcVol = volclass(self.repoPath, sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdUUID, imgUUID,
                             ancestor).getParent()

        while srcVol and dstParent != srcVol.volUUID:
            try:
                self.log.info("Teardown volume %s from image %s",
                              srcVol.volUUID, imgUUID)
                vol = srcVol.getParentVolume()
                srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID,
                                justme=True)
                srcVol = vol
            except Exception:
                self.log.info("Failure to teardown volume %s in subchain %s "
                              "-> %s", srcVol.volUUID, ancestor, successor,
                              exc_info=True)

    def removeSubChain(self, sdDom, imgUUID, chain, postZero, discard):
        """
        Remove all volumes in the sub-chain
        """
        if not chain:
            raise se.InvalidParameterException("chain", str(chain))

        volclass = sdDom.getVolumeClass()
        ancestor = chain[0]
        successor = chain[-1]
        srcVol = volclass(self.repoPath, sdDom.sdUUID, imgUUID, successor)
        dstParent = volclass(self.repoPath, sdDom.sdUUID, imgUUID,
                             ancestor).getParent()

        while srcVol and dstParent != srcVol.volUUID:
            self.log.info("Remove volume %s from image %s", srcVol.volUUID,
                          imgUUID)
            vol = srcVol.getParentVolume()
            srcVol.delete(postZero=postZero, force=True, discard=discard)
            chain.remove(srcVol.volUUID)
            srcVol = vol

    def _internalVolumeMerge(self, sdDom, srcVolParams, volParams, newSize,
                             chain):
        """
        Merge internal volume
        """
        srcVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'],
                                     volUUID=srcVolParams['volUUID'])
        # Extend successor volume to new accumulated subchain size
        srcVol.extend(newSize)

        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            backingVolPath = volume.getBackingVolumePath(
                srcVolParams['imgUUID'], volParams['volUUID'])
            srcVol.rebase(volParams['volUUID'], backingVolPath,
                          volParams['volFormat'], unsafe=False, rollback=True)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        # Prepare chain for future erase
        chain.remove(srcVolParams['volUUID'])
        self.__teardownSubChain(sdDom.sdUUID, srcVolParams['imgUUID'], chain)

        return chain

    def _baseCowVolumeMerge(self, sdDom, srcVolParams, volParams, newSize,
                            chain, discard):
        """
        Merge snapshot with base COW volume
        """
        # FIXME!!! In this case we need workaround to rebase successor
        # and transform it to be a base volume (without pointing to any backing
        # volume). Actually this case should be handled by 'qemu-img rebase'
        # (RFE to kvm). At this point we can achieve this result by 4 steps
        # procedure:
        # Step 1: create temporary empty volume similar to ancestor volume
        # Step 2: Rebase (safely) successor volume on top of this temporary
        #         volume
        # Step 3: Rebase (unsafely) successor volume on top of "" (empty
        #         string)
        # Step 4: Delete temporary volume
        srcVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'],
                                     volUUID=srcVolParams['volUUID'])
        # Extend successor volume to new accumulated subchain size
        srcVol.extend(newSize)
        # Step 1: Create temporary volume with destination volume's parent
        #         parameters
        newUUID = str(uuid.uuid4())
        sdDom.createVolume(
            imgUUID=srcVolParams['imgUUID'],
            size_blk=volParams['capacity'] // sc.BLOCK_SIZE_512,
            volFormat=volParams['volFormat'],
            preallocate=sc.SPARSE_VOL,
            diskType=volParams['disktype'],
            volUUID=newUUID,
            desc="New base volume",
            srcImgUUID=sc.BLANK_UUID,
            srcVolUUID=sc.BLANK_UUID)

        tmpVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'],
                                     volUUID=newUUID)
        tmpVol.prepare(rw=True, justme=True, setrw=True)

        # We should prepare/teardown volume for every single rebase.
        # The reason is recheckIfLeaf at the end of the rebase, that change
        # volume permissions to RO for internal volumes.
        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            # Step 2: Rebase successor on top of tmpVol
            #   qemu-img rebase -b tmpBackingFile -F backingFormat -f srcFormat
            #   src
            backingVolPath = volume.getBackingVolumePath(
                srcVolParams['imgUUID'], newUUID)
            srcVol.rebase(newUUID, backingVolPath, volParams['volFormat'],
                          unsafe=False, rollback=True)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        srcVol.prepare(rw=True, chainrw=True, setrw=True)
        try:
            # Step 3: Remove pointer to backing file from the successor by
            #         'unsafed' rebase qemu-img rebase -u -b "" -F
            #         backingFormat -f srcFormat src
            srcVol.rebase(sc.BLANK_UUID, "", volParams['volFormat'],
                          unsafe=True, rollback=False)
        finally:
            srcVol.teardown(sdUUID=srcVol.sdUUID, volUUID=srcVol.volUUID)

        # Step 4: Delete temporary volume
        tmpVol.teardown(sdUUID=tmpVol.sdUUID, volUUID=tmpVol.volUUID,
                        justme=True)
        tmpVol.delete(postZero=False, force=True, discard=discard)

        # Prepare chain for future erase
        chain.remove(srcVolParams['volUUID'])
        self.__teardownSubChain(sdDom.sdUUID, srcVolParams['imgUUID'], chain)

        return chain

    def _baseRawVolumeMerge(self, sdDom, srcVolParams, volParams, chain):
        """
        Merge snapshot with base RAW volume
        """
        # In this case we need convert ancestor->successor subchain to new
        # volume and rebase successor's children (if exists) on top of it.
        # Step 1: Create an empty volume named sucessor_MERGE similar to
        #         ancestor volume.
        # Step 2: qemuimg.convert successor -> sucessor_MERGE
        # Step 3: Rename successor to _remove_me__successor
        # Step 4: Rename successor_MERGE to successor
        # Step 5: Unsafely rebase successor's children on top of temporary
        #         volume
        srcVol = chain[-1]
        with srcVol.scopedPrepare(rw=True, chainrw=True, setrw=True):
            # Find out successor's children list
            chList = srcVolParams['children']
            # Step 1: Create an empty volume named sucessor_MERGE with
            # destination volume's parent parameters
            newUUID = srcVol.volUUID + "_MERGE"
            sdDom.createVolume(
                imgUUID=srcVolParams['imgUUID'],
                size_blk=srcVolParams['capacity'] // sc.BLOCK_SIZE_512,
                volFormat=volParams['volFormat'],
                preallocate=volParams['prealloc'],
                diskType=volParams['disktype'],
                volUUID=newUUID,
                desc=srcVolParams['descr'],
                srcImgUUID=sc.BLANK_UUID,
                srcVolUUID=sc.BLANK_UUID)

            newVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'],
                                         volUUID=newUUID)
            with newVol.scopedPrepare(rw=True, justme=True, setrw=True):
                # Step 2: Convert successor to new volume
                #   qemu-img convert -f qcow2 successor -O raw newUUID
                try:
                    operation = qemuimg.convert(
                        srcVolParams['path'],
                        newVol.getVolumePath(),
                        srcFormat=sc.fmt2str(srcVolParams['volFormat']),
                        dstFormat=sc.fmt2str(volParams['volFormat']),
                        dstQcow2Compat=sdDom.qcow2_compat(),
                        unordered_writes=sdDom.recommends_unordered_writes(
                            volParams['volFormat']))
                    with utils.stopwatch("Copy volume %s"
                                         % srcVol.volUUID):
                        self._run_qemuimg_operation(operation)
                except cmdutils.Error:
                    self.log.exception('conversion failure for volume %s',
                                       srcVol.volUUID)
                    raise se.MergeSnapshotsError(newUUID)

        if chList:
            newVol.setInternal()

        # Step 3: Rename successor as to _remove_me__successor
        tmpUUID = self.deletedVolumeName(srcVol.volUUID)
        srcVol.rename(tmpUUID)
        # Step 4: Rename successor_MERGE to successor
        newVol.rename(srcVolParams['volUUID'])

        # Step 5: Rebase children 'unsafely' on top of new volume
        #   qemu-img rebase -u -b tmpBackingFile -F backingFormat -f srcFormat
        #   src
        for ch in chList:
            ch.prepare(rw=True, chainrw=True, setrw=True, force=True)
            backingVolPath = volume.getBackingVolumePath(
                srcVolParams['imgUUID'], srcVolParams['volUUID'])
            try:
                ch.rebase(srcVolParams['volUUID'], backingVolPath,
                          volParams['volFormat'], unsafe=True, rollback=True)
            finally:
                ch.teardown(sdUUID=ch.sdUUID, volUUID=ch.volUUID)
            ch.recheckIfLeaf()

        # Prepare chain for future erase
        rmChain = [vol.volUUID for
                   vol in chain if vol.volUUID != srcVolParams['volUUID']]
        rmChain.append(tmpUUID)

        return rmChain

    def subChainSizeCalc(self, ancestor, successor, vols):
        """
        Do not add additional calls to this function.

        TODO:
        Should be unified with chainSizeCalc,
        but merge should be refactored,
        but this file should probably removed.
        """
        chain = []
        accumulated_chain_size_blk = 0
        endVolName = vols[ancestor].getParent()  # TemplateVolName or None
        currVolName = successor
        while (currVolName != endVolName):
            chain.insert(0, currVolName)
            vol_size_blk = (vols[currVolName].getVolumeSize(bs=1) //
                            sc.BLOCK_SIZE_512)
            accumulated_chain_size_blk += vol_size_blk
            currVolName = vols[currVolName].getParent()

        return accumulated_chain_size_blk, chain

    def syncVolumeChain(self, sdUUID, imgUUID, volUUID, actualChain):
        """
        Fix volume metadata to reflect the given actual chain.  This function
        is used to correct the volume chain linkage after a live merge.
        """
        curChain = self.getChain(sdUUID, imgUUID, volUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in curChain)
        self.log.info("Current chain=%s ", log_str)

        subChain = []
        for vol in curChain:
            if vol.volUUID not in actualChain:
                subChain.insert(0, vol.volUUID)
            elif len(subChain) > 0:
                break
        if len(subChain) == 0:
            return
        self.log.info("Unlinking subchain: %s", subChain)

        sdDom = sdCache.produce(sdUUID=sdUUID)
        dstParent = sdDom.produceVolume(imgUUID, subChain[0]).getParent()
        subChainTailVol = sdDom.produceVolume(imgUUID, subChain[-1])
        if subChainTailVol.isLeaf():
            self.log.info("Leaf volume %s is being removed from the chain. "
                          "Marking it ILLEGAL to prevent data corruption",
                          subChainTailVol.volUUID)
            subChainTailVol.setLegality(sc.ILLEGAL_VOL)
        else:
            for childID in subChainTailVol.getChildren():
                self.log.info("Setting parent of volume %s to %s",
                              childID, dstParent)
                sdDom.produceVolume(imgUUID, childID). \
                    setParentMeta(dstParent)

    def reconcileVolumeChain(self, sdUUID, imgUUID, leafVolUUID):
        """
        Discover and return the actual volume chain of an offline image
        according to the qemu-img info command and synchronize volume metadata.
        """
        # Prepare volumes
        dom = sdCache.produce(sdUUID)
        allVols = dom.getAllVolumes()
        imgVolumes = sd.getVolsOfImage(allVols, imgUUID).keys()
        dom.activateVolumes(imgUUID, imgVolumes)

        # Walk the volume chain using qemu-img.  Not safe for running VMs
        actualVolumes = []
        volUUID = leafVolUUID
        while volUUID is not None:
            actualVolumes.insert(0, volUUID)
            vol = dom.produceVolume(imgUUID, volUUID)
            qemuImgFormat = sc.fmt2str(vol.getFormat())
            imgInfo = qemuimg.info(vol.volumePath, qemuImgFormat)
            backingFile = imgInfo.get('backingfile')
            if backingFile is not None:
                volUUID = os.path.basename(backingFile)
            else:
                volUUID = None

        # A merge of the active layer has copy and pivot phases.
        # During copy, data is copied from the leaf into its parent.  Writes
        # are mirrored to both volumes.  So even after copying is complete the
        # volumes will remain consistent.  Finally, the VM is pivoted from the
        # old leaf to the new leaf and mirroring to the old leaf ceases. During
        # mirroring and before pivoting, we mark the old leaf ILLEGAL so we
        # know it's safe to delete in case the operation is interrupted.
        vol = dom.produceVolume(imgUUID, leafVolUUID)
        if vol.getLegality() == sc.ILLEGAL_VOL:
            actualVolumes.remove(leafVolUUID)

        # Now that we know the correct volume chain, sync the storge metadata
        self.syncVolumeChain(sdUUID, imgUUID, actualVolumes[-1], actualVolumes)

        dom.deactivateImage(imgUUID)
        return actualVolumes

    def merge(self, sdUUID, vmUUID, imgUUID, ancestor, successor, postZero,
              discard):
        """Merge source volume to the destination volume.
            'successor' - source volume UUID
            'ancestor' - destination volume UUID
        """
        self.log.info("sdUUID=%s vmUUID=%s imgUUID=%s ancestor=%s successor=%s"
                      "postZero=%s discard=%s",
                      sdUUID, vmUUID, imgUUID, ancestor, successor,
                      str(postZero), discard)
        sdDom = sdCache.produce(sdUUID)
        allVols = sdDom.getAllVolumes()
        volsImgs = sd.getVolsOfImage(allVols, imgUUID)
        # Since image namespace should be locked is produce all the volumes is
        # safe. Producing the (eventual) template is safe also.
        # TODO: Split for block and file based volumes for efficiency sake.
        vols = {}
        for vName in volsImgs.iterkeys():
            vols[vName] = sdDom.produceVolume(imgUUID, vName)

        srcVol = vols[successor]
        srcVolParams = srcVol.getVolumeParams()
        srcVolParams['children'] = []
        for vName, vol in vols.iteritems():
            if vol.getParent() == successor:
                srcVolParams['children'].append(vol)
        dstVol = vols[ancestor]
        dstParentUUID = dstVol.getParent()
        if dstParentUUID != sd.BLANK_UUID:
            volParams = vols[dstParentUUID].getVolumeParams()
        else:
            volParams = dstVol.getVolumeParams()

        accSize, chain = self.subChainSizeCalc(ancestor, successor, vols)
        image_apparent_size_blk = volParams['capacity'] // sc.BLOCK_SIZE_512
        # allocate %10 more for cow metadata
        reqSize = min(accSize, image_apparent_size_blk) * sc.COW_OVERHEAD
        try:
            # Start the actual merge image procedure
            # IMPORTANT NOTE: volumes in the same image chain might have
            # different capacity since the introduction of the disk resize
            # feature. This means that when we merge volumes the ancestor
            # should get the new size from the successor (in order to be
            # able to contain the additional data that we are collapsing).
            if dstParentUUID != sd.BLANK_UUID:
                # The ancestor isn't a base volume of the chain.
                self.log.info("Internal volume merge: src = %s dst = %s",
                              srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._internalVolumeMerge(
                    sdDom, srcVolParams, volParams, reqSize, chain)
            # The ancestor is actually a base volume of the chain.
            # We have 2 cases here:
            # Case 1: ancestor is a COW volume (use 'rebase' workaround)
            # Case 2: ancestor is a RAW volume (use 'convert + rebase')
            elif volParams['volFormat'] == sc.RAW_FORMAT:
                self.log.info("merge with convert: src = %s dst = %s",
                              srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._baseRawVolumeMerge(
                    sdDom, srcVolParams, volParams,
                    [vols[vName] for vName in chain])
            else:
                self.log.info("4 steps merge: src = %s dst = %s",
                              srcVol.getVolumePath(), dstVol.getVolumePath())
                chainToRemove = self._baseCowVolumeMerge(
                    sdDom, srcVolParams, volParams, reqSize, chain, discard)

            # This is unrecoverable point, clear all recoveries
            vars.task.clearRecoveries()
            # mark all snapshots from 'ancestor' to 'successor' as illegal
            self.markIllegalSubChain(sdDom, imgUUID, chainToRemove)
        except ActionStopped:
            raise
        except se.StorageException:
            self.log.error("Unexpected error", exc_info=True)
            raise
        except Exception as e:
            self.log.error(e, exc_info=True)
            raise se.SourceImageActionError(imgUUID, sdUUID, str(e))

        try:
            # remove all snapshots from 'ancestor' to 'successor'
            self.removeSubChain(sdDom, imgUUID, chainToRemove, postZero,
                                discard)
        except Exception:
            self.log.error("Failure to remove subchain %s -> %s in image %s",
                           ancestor, successor, imgUUID, exc_info=True)

        newVol = sdDom.produceVolume(imgUUID=srcVolParams['imgUUID'],
                                     volUUID=srcVolParams['volUUID'])
        try:
            self._shrinkVolumeToOptimalSize(newVol)
        except cmdutils.Error:
            self.log.warning("Auto shrink after merge failed", exc_info=True)

        self.log.info("Merge src=%s with dst=%s was successfully finished.",
                      srcVol.getVolumePath(), dstVol.getVolumePath())

    def _shrinkVolumeToOptimalSize(self, vol):
        if not vol.chunked():
            return

        vol.prepare()
        try:
            optimal_size = vol.optimal_size()
        finally:
            vol.teardown(vol.sdUUID, vol.volUUID)

        optimal_size_blk = optimal_size // sc.BLOCK_SIZE
        vol.reduce(optimal_size_blk)

    def _activateVolumeForImportExport(self, domain, imgUUID, volUUID=None):
        chain = self.getChain(domain.sdUUID, imgUUID, volUUID)
        log_str = logutils.volume_chain_to_str(vol.volUUID for vol in chain)
        self.log.info("chain=%s ", log_str)

        template = chain[0].getParentVolume()
        if template or len(chain) > 1:
            self.log.error("Importing and exporting an image with more "
                           "than one volume is not supported")
            raise se.CopyImageError()

        domain.activateVolumes(imgUUID, volUUIDs=[chain[0].volUUID])
        return chain[0]

    def upload(self, methodArgs, sdUUID, imgUUID, volUUID=None):
        domain = sdCache.produce(sdUUID)

        vol = self._activateVolumeForImportExport(domain, imgUUID, volUUID)
        try:
            imageSharing.upload(vol.getVolumePath(), methodArgs)
        finally:
            domain.deactivateImage(imgUUID)

    def download(self, methodArgs, sdUUID, imgUUID, volUUID=None):
        domain = sdCache.produce(sdUUID)

        vol = self._activateVolumeForImportExport(domain, imgUUID, volUUID)
        try:
            # Extend the volume (if relevant) to the image size
            img_size_blk = imageSharing.getSize(methodArgs) / sc.BLOCK_SIZE
            vol.extend(img_size_blk)
            imageSharing.download(vol.getVolumePath(), methodArgs)
        finally:
            domain.deactivateImage(imgUUID)

    def copyFromImage(self, methodArgs, sdUUID, imgUUID, volUUID):
        domain = sdCache.produce(sdUUID)

        vol = self._activateVolumeForImportExport(domain, imgUUID, volUUID)
        try:
            imageSharing.copyFromImage(vol.getVolumePath(), methodArgs)
        finally:
            domain.deactivateImage(imgUUID)

    def copyToImage(self, methodArgs, sdUUID, imgUUID, volUUID=None):
        domain = sdCache.produce(sdUUID)

        vol = self._activateVolumeForImportExport(domain, imgUUID, volUUID)
        try:
            # Extend the volume (if relevant) to the image size
            img_size_blk = (imageSharing.getLengthFromArgs(methodArgs) /
                            sc.BLOCK_SIZE)
            vol.extend(img_size_blk)
            imageSharing.copyToImage(vol.getVolumePath(), methodArgs)
        finally:
            domain.deactivateImage(imgUUID)
