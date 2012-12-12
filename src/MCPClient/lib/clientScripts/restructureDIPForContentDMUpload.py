#!/usr/bin/python -OO

# This file is part of Archivematica.
#
# Copyright 2010-2012 Artefactual Systems Inc. <http://artefactual.com>
#
# Archivematica is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Archivematica is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Archivematica.  If not, see <http://www.gnu.org/licenses/>.

# @package Archivematica
# @subpackage archivematicaClientScript
# @author Mark Jordan <mark2jordan@gmail.com>

import argparse
import os
import sys
import glob
import shutil
import json
import urllib
import csv
import collections
import zipfile
import re
from xml.dom.minidom import parse, parseString
sys.path.append("/usr/lib/archivematica/archivematicaCommon")
from archivematicaFunctions import normalizeNonDcElementName

# Create the output dir for the CONTENTdm DIP and return the resulting path.
# importMethod is either 'projectclient' or 'directupload'.
def prepareOutputDir(outputDipDir, importMethod, dipUuid):
    outputDipDir = os.path.join(outputDipDir, 'CONTENTdm', importMethod, dipUuid)
    # Check for and then delete a subdirectory named after the current package. We always want
    # a clean output directory for the import package.
    if os.path.exists(outputDipDir):
        shutil.rmtree(outputDipDir)
    os.makedirs(outputDipDir)
    return outputDipDir


# Takes in a DOM object containing the DC or OTHER dmdSec, returns a dictionary with 
# tag : [ value1, value2] members. Also, since minidom only handles byte strings
# so we need to encode strings before passing them to minidom functions. label is
# an optional arguement for use with compound item children, which may not have a
# dublincore object.
def parseDmdSec(dmdSec, label = '[Placeholder title]'):
    # If the dmdSec object is empty (i.e, no DC metadata has been assigned
    # in the dashboard, and there was no metadata.csv or other metadata file
    # in the transfer), return a placeholder title.
    if dmdSec is None:
        return {'title' : [label]}
    if not hasattr(dmdSec, 'getElementsByTagName'):
        return {'title' : [label]}    

    mdWraps = dmdSec.getElementsByTagName('mdWrap')
    mdType = mdWraps[0].attributes["MDTYPE"]
    
    # If we are dealing with a DOM object representing the Dublin Core metadata,
    # check to see if there is a title (required by CONTENTdm). If not, assign a 
    # placeholder title and return.
    if mdType == 'DC' and hasattr(dmdSec, 'getElementsByTagName'):
        dcTitlesDom = dmdSec.getElementsByTagName('title')
        if not dcTitlesDom:
            return {'title' : '[Placeholder title]'} 

    # Get the elements found in the incoming XML DOM object.
    elementsDom = dmdSec.getElementsByTagName('*')
    elementsDict = {}
    for element in elementsDom:
        # We only want elements that are not empty.
        if element.firstChild: 
            # To get the values of repeated elements, we need to create a list to correspond
            # to each element name. If the element name is not yet a key in elementsDict,
            # create the element's value list.
            if element.tagName not in elementsDict:
                elementsDict[element.tagName.encode("utf-8")] = [element.firstChild.nodeValue.encode("utf-8")]
            # If the element name is present in elementsDict, append the element's value to
            # its value list.
            else:
                elementsDict[element.tagName.encode("utf-8")].append(element.firstChild.nodeValue.encode("utf-8"))
    
    # Before we return elementsDict, remove the items that are simple METS wrappers and are not metadata elements.
    del elementsDict['mdWrap']
    del elementsDict['xmlData']
    return elementsDict


# Takes in a DOM object containing the METS structMap, returns a dictionary with 
# fptrValue : [ order, parent, dmdSec, label, filename ] members.
# Files in the DIP objects directory start with the UUID (i.e., first 36 characters 
# of the filename) # of the of the file named in the fptr FILEID in the structMap; 
# each file ends in the UUID. Also, we are only interested in divs that are direct
# children of a div with TYPE=directory and LABEL=objects:
#  <div TYPE="directory" LABEL="DigitizationOutput-50a3c71f-92d6-46d1-98ce-8227caa79f85-50a3c71f-92d6-46d1-98ce-8227caa79f85">
#     <div TYPE="directory" LABEL="objects" DMDID="dmdSec_1">
#       <div LABEL="Page 1">
#         <fptr FILEID="P1050152.JPG-e2d0cd78-f1b9-446b-81ae-ea0e282332bb"/>
#       </div>
def parseStructMap(structMap, filesInObjectDirectory):
    structMapDict = {}
    # Get filenames of all the files in the objects directory (recursively);
    # filesInObjectDirectory contains paths, we need to get the filename only
    # for the structMap checking. Add each filename to structMapDict.
    filesInObjectDir = []
    for file in filesInObjectDirectory:
        if file is not None:
            head, tail = os.path.split(file)
            filesInObjectDir.append(tail)
        
    # Get all the fptr elements.
    fptrOrder = 0
    for node in structMap.getElementsByTagName('fptr'):
        for k, v in node.attributes.items():
            if k == 'FILEID':
                # parentDivDmdId is a placeholder for when we support compound
                # items with their own descriptive metadata.
                parentDivDmdId = node.parentNode.getAttribute('DMDID')
                filename = getFptrObjectFilename(v, filesInObjectDir)
                # We only want entries for files that are in the objects directory.
                if filename is not None:
                    parentDivLabel = node.parentNode.getAttribute('LABEL')
                    # If the parent div doesn't have a LABEL, use the filesname as the label.
                    if not len(parentDivLabel):
                        parentDivLabel = filename
                    fptrOrder = fptrOrder + 1
                    structMapDict[v] = {
                        # Python has no natsort, so we padd fptOrder with up to
                        # 4 zeros to make it more easily sortable.
                        'order' : str(fptrOrder).zfill(5),
                        'parent' : '', # Placeholder for when we support hierarchical items.
                        'filename' : filename,
                        'label' : parentDivLabel,
                        'dmdSec' : parentDivDmdId
                    }

    return structMapDict


# Given a ftpr FILEID value (which looks like this: P1050154.JPG-09869659-fc89-46ce-ad1c-fe166becccca),
# return the name of the corresponding file from the DIP objects directory.
def getFptrObjectFilename(fileId, filesInObjectDir):
    # Assumes UUID is the last 36 characters of the fptr value.
    uuid = fileId[-36:]
    for filename in filesInObjectDir:
        if uuid in filename:
            return filename


# Generate a dictionary containing 1) 'dcMappings', a nested dictionary with DCTERMS
# elememts as keys, each of which has as its values the CONTENTdm nick and name for
# the corresponding field in the current collection and 2), 'nonDcMappings', a nested
# disctionary with field names (i.e., labels) as keys, each of which has as its values
# the CONTENTdm nick and name for the corresponding field in the collection, and 3), 
# 'order', a list of the collection's field nicks in the order they exist in the
# collection's configuration, which is needed to write out the metadata in the correct
# field order. The Archivematica metadata CRUD form only uses the legacy unqualified
# DC elements but we include the entire CONTENTdm DCTERMS mappings because the entire
# set of DCTERMS are supported in dublincore.xml files included in the transfer
# package's metadata directory and in bulk transfer metadata.csv files.
def getContentdmCollectionFieldInfo(contentdmServer, targetCollection):
    collectionFieldInfo = {}
    # First, define the CONTENTdm DC nicknames -> DCTERMs mapping. 
    contentdmDctermsMap = {
         'describ' : 'abstract',
         'rightsa' : 'accessRights',
         'accrua' : 'accrualMethod',
         'accrub' : 'accrualPeriodicity',
         'accruc' : 'accrualPolicy',
         'titlea' : 'alternative',
         'audien' : 'audience',
         'datec' : 'available',
         'identia' : 'bibliographicCitation',
         'relatim' : 'conformsTo',
         'contri' : 'contributor',
         'covera' : 'coverage',
         'datea' : 'created',
         'creato' : 'creator',
         'date' : 'date',
         'datef' : 'dateAccepted',
         'dateg' : 'dateCopyrighted',
         'dateh' : 'dateSubmitted',
         'descri' : 'description',
         'audienb' : 'educationLevel',
         'formata' : 'extent',
         'format' : 'format',
         'relatil' : 'hasFormat',
         'relatih' : 'hasPart',
         'relatib' : 'hasVersion',
         'identi' : 'identifier',
         'instru' : 'instructionalMethod',
         'relatik' : 'isFormatOf',
         'relatig' : 'isPartOf',
         'relatii' : 'isReferencedBy',
         'relatic' : 'isReplacedBy',
         'relatie' : 'isRequiredBy',
         'relatia' : 'isVersionOf',
         'dated' : 'issued',
         'langua' : 'language',
         'rightsb' : 'license',
         'audiena' : 'mediator',
         'formatb' : 'medium',
         'datee' : 'modified',
         'proven' : 'provenance',
         'publis' : 'publisher',
         'relatij' : 'references',
         'relati' : 'relation',
         'relatid' : 'replaces',
         'relatif' : 'requires',
         'rights' : 'rights',
         'rightsc' : 'rightsHolder',
         'source' : 'source',
         'coveraa' : 'spatial',
         'subjec' : 'subject',
         'descria' : 'tableOfContents',
         'coverab' : 'temporal',
         'title' : 'title',
         'type' : 'type',
         'dateb' : 'valid',
    }
    # Query CONTENTdm to get the target collection's field configurations.
    CollectionFieldConfigUrl = contentdmServer + '?q=dmGetCollectionFieldInfo' + targetCollection + '/json'
    try:
        f = urllib.urlopen(CollectionFieldConfigUrl)
        collectionFieldConfigString = f.read()
        collectionFieldConfig = json.loads(collectionFieldConfigString)
    except:
        print "Cannot retrieve CONTENTdm collection field configuration from " + CollectionFieldConfigUrl
        sys.exit(1)

    # For the DC mappings, we want a dict containing items that looks like
    # { 'contributor': { 'name': u'Contributors', 'nick': u'contri'},
    # 'creator': { 'name': u'Creator', 'nick': u'creato'},
    # 'date': { 'name': u'Date', 'nick': u'dateso'}, [...] }
    # We need these field-specific mappings when writing out metadata files for loading
    # into CONTENTdm. It is possible that more than one CONTENTdm field is mapped to
    # the same DC element; in this case, just take the last mapping and ignore the rest,
    # since there is no way to tell which should take precedence. The non-DC mappings have
    # the field name as their key, like "u'CONTENTdm number': { 'name': u'CONTENTdm number',
    # 'nick': u'dmrecord'} (i.e., key and 'name' are the same).
    collectionFieldDcMappings = {}
    collectionFieldNonDcMappings = {}
    # We also want a simple list of all the fields in the current collection, in the order
    # they exist in the collection's CONTENTdm configuration.
    collectionFieldOrder = []
    # Define a set of CONTENTdm-generated fields that we don't want to show up in the mappings.
    systemFields = ['fullrs', 'dmoclcno', 'dmcreated', 'dmmodified', 'dmrecord', 'find']
    for fieldConfig in collectionFieldConfig:
        for k, v in fieldConfig.iteritems():
            fieldName = fieldConfig['name']
            # For fields that have a DC mapping.
            if fieldConfig['dc'] != 'BLANK' and fieldConfig['dc'] != '':
                collectionFieldDcMappings[contentdmDctermsMap[fieldConfig['dc']]] = {'nick' : fieldConfig['nick'] , 'name' : fieldName}
            # For fields that do not have a DC mapping.   
            if fieldConfig['dc'] == 'BLANK':
                collectionFieldNonDcMappings[fieldName] = {'nick' : fieldConfig['nick'] , 'name' : fieldName}
            # Testing: make all the fields to be considered non-DC.
            # collectionFieldNonDcMappings[fieldName] = {'nick' : fieldConfig['nick'] , 'name' : fieldName}
        if fieldConfig['nick'] not in systemFields:
            collectionFieldOrder.append(fieldConfig['nick'])
    collectionFieldInfo['dcMappings'] = collectionFieldDcMappings
    collectionFieldInfo['nonDcMappings'] = collectionFieldNonDcMappings
    collectionFieldInfo['order'] = collectionFieldOrder
    return collectionFieldInfo


# Return the dmdSec with the specific ID value. If dublinCore is True, return
# the <dublincore> child node only.
def getDmdSec(metsDom, dmdSecId = 'dmdSec_1', dublinCore = True):
    for node in metsDom.getElementsByTagName('dmdSec'):
        for k, v in node.attributes.items():
            if dublinCore and k == 'ID' and v == dmdSecId:
                # Assumes there is only one dublincore child element.
                return node.getElementsByTagName('dublincore')[0]
            else:
                return node


# Get a list of all the files (recursive) in the DIP object directory. Even though there
# can be subdirectories in the objects directory, assumes each file should have a unique name.
def getObjectDirectoryFiles(objectDir):
    fileList = []
    for root, subFolders, files in os.walk(objectDir):
        for file in files:
            fileList.append(os.path.join(root, file))
    return fileList


# Create a .zip from the DIP files produced by generateXXProjectClientPackage functions.
# Resulting zip file is written to the uploadedDIPs directory.
def zipProjectClientOutput(outputDipDir, zipOutputDir, dipUuid):
    currentDir = os.getcwd()
    # We want to chdir to this directory so we can only include the DIP-specific
    # structure in our zip file.
    zipOutputPath = os.path.join(zipOutputDir, 'CONTENTdm', 'projectclient')
    os.chdir(zipOutputPath)
    zipOutputFile = dipUuid + '.zip'
    # zipOutputFile is now relative to zipOutputPath since we have chdir'ed here.
    outputFile = zipfile.ZipFile(zipOutputFile, "w")
    # Because we have chdir'ed, we use the relative dipUuid as the source directory
    # for our zip file.
    for dirpath, dirnames, filenames in os.walk(dipUuid):
        for filename in filenames:
            outputFile.write(os.path.join(dirpath, filename), os.path.join(dirpath, filename))
    outputFile.close()
    os.chdir(currentDir)


# Generate a .desc file used in CONTENTdm 'direct import' packages. Use dcMetadata only
# if nonDcMetadata is empty.
# .desc file looks like this:
# <?xml version="1.0" encoding="utf-8"?>
# <itemmetadata>
# <title>wall</title>
#  [... every collection field nick, empty and with values]
# <is></is>
# <transc></transc>
# <fullrs />
# <dmoclcno></dmoclcno>
# <dmcreated></dmcreated>
# <dmmodified></dmmodified>
# <dmrecord></dmrecord>
# <find></find>
# <dmimage></dmimage>
# <dmad1></dmad1>
# <dmad2></dmad2>
# <dmaccess></dmaccess>
# </xml>
def generateDescFile(dcMetadata, nonDcMetadata):
    collectionFieldInfo = getContentdmCollectionFieldInfo(args.contentdmServer, args.targetCollection)
    output = '<?xml version="1.0" encoding="utf-8"?>' + "\n"
    output += "<itemmetadata>\n"

    # Process the non-DC metadata, if there is any.
    if nonDcMetadata != None:
        # Define a list of elements we don't want to add based on their presence in the collection's
        # field config, since we add them in the template at the end of this function.
        doNotAdd = ['transc', 'fullrs', 'dmoclcno', 'dmcreated', 'dmmodified', 'dmrecord',
            'find', 'dmimage', 'dmad1', 'dmad2', 'dmaccess']
        for element in collectionFieldInfo['nonDcMappings'].keys():
            # If a field is in the incoming item non-DC metadata, populate the corresponding tag
            # with its 'nick' value.
            # First, normalize CONTENTdm field names so they can match element names in the
            # metadata. We need to do this because the raw (i.e., human readable field names)
            # are used as keys in collectionFieldInfo for fields that are not mapped to DC.
            normalizedElement = normalizeNonDcElementName(element)
            if normalizedElement in nonDcMetadata.keys():
                values = ''
                output += '<' + collectionFieldInfo['nonDcMappings'][element]['nick'] + '>'
                # Repeated values in CONTENTdm metadata need to be separated with semicolons.
                for value in nonDcMetadata[normalizedElement]:
                    values += value + '; '
                    output += values.rstrip('; ')
                output += '</' + collectionFieldInfo['nonDcMappings'][element]['nick'] + ">\n"
            # We need to include elements that are in the collection field config but
            # that do not have any values for the current item.
            else:
                if collectionFieldInfo['nonDcMappings'][element]['nick'] not in doNotAdd:
                    output += '<' + collectionFieldInfo['nonDcMappings'][element]['nick'] + '></' + collectionFieldInfo['nonDcMappings'][element]['nick'] + ">\n"

    # I.e., there is no nonDcMetadata.
    else:
        # Process DC metadata first. Loop through the collection's field configuration and generate
        # XML elements for all its fields. 
        for dcElement in collectionFieldInfo['dcMappings'].keys():
            # If a field is in the incoming item dcMetadata, populate the corresponding tag
            # with its 'nick' value.
            if dcElement in dcMetadata.keys():
                values = ''
                output += '<' + collectionFieldInfo['dcMappings'][dcElement]['nick'] + '>'
                # Repeated values in CONTENTdm metadata need to be separated with semicolons.
                for value in dcMetadata[dcElement]:
                    values += value + '; '
                    output += values.rstrip('; ')
                output += '</' + collectionFieldInfo['dcMappings'][dcElement]['nick'] + ">\n"
            # We need to include elements that are in the collection field config but
            # that do not have any values for the current item.
            else:
                output += '<' + collectionFieldInfo['dcMappings'][dcElement]['nick'] + '></' + collectionFieldInfo['dcMappings'][dcElement]['nick'] + ">\n"

    # These fields are boilerplate in new .desc files.          
    output += "<transc></transc>\n"
    output += "<fullrs />\n"
    output += "<dmoclcno></dmoclcno>\n"
    output += "<dmcreated></dmcreated>\n"
    output += "<dmmodified></dmmodified>\n"
    output += "<dmrecord></dmrecord>\n"
    output += "<find></find>\n"
    output += "<dmimage></dmimage>\n"
    output += "<dmad1></dmad1>\n"
    output += "<dmad2></dmad2>\n"
    output += "<dmaccess></dmaccess>\n"
    output += "</xml>\n"
    return output


# Generate an object file's entry in the .full file.
def generateFullFileEntry(title, filename, extension):
    fullFileContent = "<item>\n"
    fullFileContent += "  <title>" + title + "</title>\n"
    fullFileContent += "  <object>" + filename + extension + "</object>\n"
    fullFileContent += "  <desc>" + filename + ".desc</desc>\n"
    fullFileContent += "  <icon>" + filename + ".icon</icon>\n"
    fullFileContent += "  <update>0</update>\n  <info>nopdf</info>\n"
    fullFileContent += "</item>\n"
    return fullFileContent


# Takes in a DOM object 'structMap' and determines if it describes simple or compound 
# items by finding the div in structMaps[0] that contains the DMDID value "dmdSec_1",
# and then getting the value of that div's TYPE attribute; if it's 'item', the item
# is simple, if it's 'directory', the item is compound.
# @todo: Account for no DMDID.
def getItemCountType(structMap):
    for node in structMap.getElementsByTagName('div'):
        for k, v in node.attributes.items():
            # We use a regex to cover 'dmdSec_1' or 'dmdSec_1 dmdSec_2'.
            match = re.search(r'dmdSec_1', v)
            if k == 'DMDID' and match:
                # Get the value of the TYPE attribute.
                type = node.attributes["TYPE"]
                if type.value == 'item':
                    return 'simple'
                if type.value == 'directory':
                    return 'compound'


# Given all the dmdSecs (which are DOM objects) from a METS files, group the dmdSecs
# into item-specific pairs (for DC and OTHER) or if OTHER is not present, DC. Returns
# a list of lists, with each list containing one or two dmdSec DOM nodes.
def groupDmdSecs(dmdSecs):
    groupedDmdSecs = list()
    dmdSecsLen = len(dmdSecs)
    # If dmdSecs is empty, return.
    if dmdSecsLen == 0:
        return groupedDmdSecs
        
    # If dmdSecs is not empty, test whether the first dmdSec has MDTYPE="OTHER"; if
    # this is the case, we can assume that the dmdSecs need to be grouped into groups
    # of 2; if this is not the case, we can assume that the dmdSecs need to be grouped
    # into groups of 1. We also check to see whether the second dmdSec has MDTYPE="DC";
    # this assumes there will be some DC elements for every item in the DIP.
    
    # Before we do that, check to see if we only have one dmdSec.
    if dmdSecsLen == 1:
        tmpList = list()
        tmpList.append(dmdSecs[0])
        groupedDmdSecs.append(tmpList)
        return groupedDmdSecs
    
    # If we've made it this far, perform the test on the second dmdSec.
    mdWrap = dmdSecs[1].getElementsByTagName('mdWrap')[0]
    secondDmdSecType = mdWrap.attributes['MDTYPE'].value
    if secondDmdSecType == 'OTHER':
        groupSize = 1
    if secondDmdSecType == 'DC':
        groupSize = 2
             
    # Loop through all the dmdSecs and pop them off in chuncks so we can
    # group them. 
    count = 0
    while (count < dmdSecsLen):
        count = count + 1
        if groupSize == 1:
            tmpList = list()
            firstDmdSec = dmdSecs.pop(0)
            tmpList.append(firstDmdSec)
            groupedDmdSecs.append(tmpList)
        # We need to check to make sure we don't reduce the number of
        # dmdSecs down to 0.
        if groupSize == 2 and len(dmdSecs) >= dmdSecsLen / 2:
            tmpList = list()
            firstDmdSec = dmdSecs.pop(0)
            tmpList.append(firstDmdSec)
            secondDmdSec = dmdSecs.pop(0)
            tmpList.append(secondDmdSec)
            groupedDmdSecs.append(tmpList)
     
    return groupedDmdSecs


# Given a group of two dmdSecs, split them out so they can be passed to
# generateDescFile() with the expected values.
def splitDmdSecs(dmdSecs):
    lenDmdSecs = len(dmdSecs)
    dmdSecPair = dict()
    if lenDmdSecs == 2:
        for dmdSec in dmdSecs:
            mdWrap = dmdSec.getElementsByTagName('mdWrap')[0]
            if mdWrap.attributes['MDTYPE'].value == 'OTHER':
                dmdSecPair['nonDc'] = parseDmdSec(dmdSec)
            if mdWrap.attributes['MDTYPE'].value == 'DC':
                dmdSecPair['dc'] = parseDmdSec(dmdSec)
    if lenDmdSecs == 1:
        mdWrap = dmdSecs[0].getElementsByTagName('mdWrap')[0]
        if mdWrap.attributes['MDTYPE'].value == 'OTHER':
            dmdSecPair['nonDc'] = parseDmdSec(dmdSec)
            dmdSecPair['dc'] = None
        if mdWrap.attributes['MDTYPE'].value == 'DC':
            dmdSecPair['dc'] = parseDmdSec(dmdSec)
            dmdSecPair['nonDc'] = None
    if lenDmdSecs == 0:
        # If dmdSecs is empty, let parseDcXML() assign a placeholder title in dcMetadata.
        dmdSec = dmdSecs
        dmdSecPair['dc'] = parseDmdSec(dmdSec)
        dmdSecPair['nonDc'] = None

    return dmdSecPair


# Given a list of structMaps and a DMDID value, return a list of all the
# <fptr> values for the files named in the structMap corresponding to
# to the DMDID.
def getFileIdsForDmdSec(structMaps, dmdSecIdValue):
    dmdSecIdValue = dmdSecIdValue.strip()
    fileIds = []
    # We use the Archivematica default structMap, which is always the first.
    structMap = structMaps[0]
    for div in structMap.getElementsByTagName('div'):
        for k, v in div.attributes.items():
            # We match on the first dmdSec ID. Space is optional because 
            # there could be two dmdSec IDs in the value.
            match = re.search(r'%s\s?$' % dmdSecIdValue, v)
            if k == 'DMDID' and match:
                for fptr in div.getElementsByTagName('fptr'):
                    for k, v in fptr.attributes.items():
                        if k == 'FILEID':
                            fileIds.append(v)
                            
    return fileIds


# Given a group of dmdSecs and the METS structMaps, return a list of files
# that are described by the dmdSecs.
def getFilesInObjectDirectoryForThisDmdSecGroup(dmdSecGroup, structMaps):
    filesInObjectDirectoryForThisDmdSecGroup = list()
    # Get the value of ID for each <dmdSec> and put them in a list,
    # then pass the list into getFileIdsForDmdSec()
    for dmdSec in dmdSecGroup:
        Id = dmdSec.attributes['ID']
        fileIds = getFileIdsForDmdSec(structMaps, Id.value)
        for fileId in fileIds:
            filename = getFptrObjectFilename(fileId, filesInObjectDirectory)
            if filename is not None:
                filesInObjectDirectoryForThisDmdSecGroup.append(filename)
            
    return filesInObjectDirectoryForThisDmdSecGroup
    

# Generate a 'direct upload' package for a simple item from the Archivematica DIP.
# This package will contain the object file, its thumbnail, a .desc (DC metadata) file,
# and a .full (manifest) file.
def generateSimpleContentDMDirectUploadPackage(dmdSecs, structMaps, dipUuid, outputDipDir, filesInObjectDirectoryForThisDmdSec, filesInThumbnailDirectory):
    dmdSecPair = splitDmdSecs(dmdSecs)
    # (nonDcMetadata, dcMetadata) = splitDmdSecs(dmdSecs)
    descFileContents = generateDescFile(dmdSecPair['dc'], dmdSecPair['nonDc'])
    
    # Get the object base filename and extension. Since we are dealing with simple items,
    # there should only be one file in filesInObjectDirectoryForThisDmdSec.
    objectFilePath, objectFileFilename = os.path.split(filesInObjectDirectoryForThisDmdSec[0])
    objectFileBaseFilename, objectFileExtension = os.path.splitext(objectFileFilename)    
    
    # Write the .desc file into the output directory.
    descFile = open(os.path.join(outputDipDir, objectFileBaseFilename + '.desc'), "wb")
    descFile.write(descFileContents)
    descFile.close()
    
    # Copy the object file into the output directory.
    objectFileDest = os.path.join(outputDipDir, objectFileBaseFilename + objectFileExtension)
    shutil.copy(filesInObjectDirectoryForThisDmdSec[0], objectFileDest)

    # Copy the thumbnail into the output directory. The file must end in .icon.
    # The thumbnail filenames in the DIP use the corresponding file's UUID (i.e.,
    # the first 36 characters of the object file's base name).
    thumbnailFilename = objectFileBaseFilename[:36] + '.jpg'
    for thumbnailPath in filesInThumbnailDirectory:
        match = re.search(r'%s$' % thumbnailFilename, thumbnailPath)
        if match:
            shutil.copy(thumbnailPath, os.path.join(outputDipDir, objectFileBaseFilename + '.icon'))

    fullFileContents = generateFullFileEntry(objectFileBaseFilename + objectFileExtension, objectFileBaseFilename, objectFileExtension)
    fullFile = open(os.path.join(outputDipDir, objectFileBaseFilename + '.full'), "wb")
    fullFile.write(fullFileContents)
    fullFile.close()


# Generate a 'project client' package for a simple item from the Archivematica DIP.
# This package will contain the object file and a delimited metadata file in a format
# suitable for importing into CONTENTdm using its Project Client.
def generateSimpleContentDMProjectClientPackage(dmdSecs, structMaps, dipUuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup):
    dmdSecPair = splitDmdSecs(dmdSecs)
    nonDcMetadata = dmdSecPair['nonDc']
    dcMetadata = dmdSecPair['dc']
    collectionFieldInfo = getContentdmCollectionFieldInfo(args.contentdmServer, args.targetCollection)

    # Since we are dealing with simple objects, there should only be one file
    # in filesInObjectDirectoryForThisDmdSec. Copy it into the output directory.
    shutil.copy(filesInObjectDirectoryForThisDmdSec[0], outputDipDir)
    # Get the object filename, which we will add to the delimited file below.
    path, filename = os.path.split(filesInObjectDirectoryForThisDmdSecGroup[0])
      
    # Populate a row to write to the metadata file, with the first row containing the
    # field labels and the second row containing the values. Both rows needs to be
    # in the order expressed in collectionFieldInfo['order']. For each item in
    # collectionFieldInfo['order'], query each mapping in collectionFieldInfo['mappings']
    # to find a matching 'nick'; if the nick is found, write the value in the dmdSec's
    # element that matches the mapping's key; if no matching mapping is found, write ''.
    # The DIP filename (in this case, the file variable defined above) needs to go in
    # the last column.
    delimHeaderRow = []
    delimValuesRow = []

    for field in collectionFieldInfo['order']:
        # Process the non-DC metadata, if there is any.
        if nonDcMetadata != None:
            # for k, v in collectionFieldInfo['dcMappings'].iteritems():
            for k, v in collectionFieldInfo['nonDcMappings'].iteritems():
                if field == v['nick']:
                    # Append the field name to the header row.
                    delimHeaderRow.append(v['name'])
                    # Append the element value to the values row.
                    if normalizeNonDcElementName(k) in nonDcMetadata:
                        # In CONTENTdm, repeated values are joined with a semicolon.
                        normalized_name = normalizeNonDcElementName(k)
                        joinedNonDcMetadataValues = '; '.join(nonDcMetadata[normalized_name])                   
                        # Rows can't contain new lines.
                        joinedNonDcMetadataValues = joinedNonDcMetadataValues.replace("\r","")
                        joinedNonDcMetadataValues = joinedNonDcMetadataValues.replace("\n","")
                        delimValuesRow.append(joinedNonDcMetadataValues)
                    # Append a placeholder to keep the row intact.
                    else:
                        delimValuesRow.append('')
        # I.e., there is no nonDcMetadata.
        else:
            for k, v in collectionFieldInfo['dcMappings'].iteritems():
                if field == v['nick']:
                    # Append the field name to the header row.
                    delimHeaderRow.append(v['name'])
                    # Append the element value to the values row.
                    if k in dcMetadata:
                        # In CONTENTdm, repeated values are joined with a semicolon.
                        joinedDcMetadataValues = '; '.join(dcMetadata[k])                   
                        # Rows can't contain new lines.
                        joinedDcMetadataValues = joinedDcMetadataValues.replace("\r","")
                        joinedDcMetadataValues = joinedDcMetadataValues.replace("\n","")
                        delimValuesRow.append(joinedDcMetadataValues)
                    # Append a placeholder to keep the row intact.
                    else:
                        delimValuesRow.append('')

    # Wite out a tab-delimited file containing the DC-mapped metadata,
    # with 'Filename' as the last field.
    simpleTxtFilePath = os.path.join(outputDipDir, 'simple.txt')   
    # Check to see if simple.txt already exists, and if it does, append delimValuesRow
    # to it.
    if os.path.exists(simpleTxtFilePath):
        delimitedFile = open(simpleTxtFilePath, "ab")
        writer = csv.writer(delimitedFile, delimiter='\t')
    # If it doesn't exist yet, write out the header row.
    else:
        delimitedFile = open(simpleTxtFilePath, "wb")
        writer = csv.writer(delimitedFile, delimiter='\t')
        delimHeaderRow.append('Filename') # Must contain 'Filename' in last position
        writer.writerow(delimHeaderRow)
        
    # Write out the object filename. The filename must be in the last field in the row.
    delimValuesRow.append(filename)
    
    # Write the values row and close the file.
    writer.writerow(delimValuesRow)
    delimitedFile.close()


# Generate a 'direct upload' package for a compound item from the Archivematica DIP.
# Consults the structMap and write out a corresponding structure (.cpd) file. Also,
# for every file, copy the file, create an .icon, create a .desc file, plus create
# index.desc, index.cpd, index.full, and ready.txt. @todo: If a user-submitted
# structMap is present, use it to order the files.
def generateCompoundContentDMDirectUploadPackage(dmdSecs, structMaps, dipUuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup, filesInThumbnailDirectory):
    dmdSecPair = splitDmdSecs(dmdSecs)
    descFileContents = generateDescFile(dmdSecPair['dc'], dmdSecPair['nonDc'])
    # Make a copy of nonDcMetadata that we use for compound item children (see comment below).
    nonDcMetadataForChildren = nonDcMetadata

    # Each item needs to have its own directory under outputDipDir. Since these item-level directories
    # will end up in CONTENTdm's import/cdoc directory, they need to be unique; therefore, we can't use the
    # dmdSec IDs, which are not unique across DIPs. To supply a unique UUID for each compound item, we use
    # the the first eight characters of the UUID of the first file in each compound item.
    firstFilePath, firstFileFilename = os.path.split(filesInObjectDirectoryForThisDmdSecGroup[0])
    itemDirUuid = firstFileFilename[:8]
    outputItemDir = os.path.join(outputDipDir, itemDirUuid)
    os.mkdir(outputItemDir)
    
    # Output a .desc file for the parent item (index.desc).
    descFile = open(os.path.join(outputItemDir, 'index.desc'), "wb")
    descFile.write(descFileContents)
    descFile.close()

    # Start to build the index.cpd file.
    # @todo: <type> will be 'Monograph' for hierarchical items.
    cpdFileContent = "<cpd>\n  <type>Document</type>\n"

    # Start to build the index.full file.
    fullFileContent = ''
    # Populate the 'full' elements for the parent item.
    titleValues = ''
    for titleValue in dcMetadata['title']:
        titleValues += titleValue + '; '
    titleValues = titleValues.rstrip('; ')
    fullFileContents = generateFullFileEntry(titleValues, 'index', '.cpd')

    # Archivematica's stuctMap is always the first one; the user-submitted structMap
    # is always the second one. @todo: If the user-submitted structMap is present,
    # parse it for the SIP structure so we can use that structure in the CONTENTdm packages.
    structMapDom =  metsDom.getElementsByTagName('structMap')[0]
    structMapDict = parseStructMap(structMapDom, filesInObjectDirectoryForThisDmdSecGroup)

    # Determine the order in which we will add the child-level rows to the .cpd and .full files.
    Orders = []
    for fptr, details in structMapDict.iteritems():
        Orders.append(details['order'])

    # Iterate through the list of order values and add the matching structMapDict entry
    # to the delimited file (and copy the file into the scans directory).
    for order in sorted(Orders):
        for k, v in structMapDict.iteritems():
            # Get each access file's base filesname without extension, since we'll use it
            # for the .icon and .desc files.
            accessFileBasenameName, accessFileBasenameExt = os.path.splitext(v['filename'])

            # Get the name of the first file in the sorted order; we use this later to create
            # a thumbnail for current parent item.
            if v['order'] == '00001':
                parentThumbnailFilename = accessFileBasenameName + '.icon' 

            if order == v['order']:
               # Process each object file.
               for fullPath in filesInObjectDirectoryForThisDmdSecGroup:
                   # For each object file, output the object file. We need to find the full path
                   # of the file identified in v['filename'].
                   if (v['filename'] in fullPath):
                       shutil.copy(fullPath, outputItemDir)

                   # For each object file, copy the thumbnail in the DIP to the import package.
                   # The file must have the same name as the object file but it must end in .icon.
                   for thumbnailFilePath in filesInThumbnailDirectory:
                       thumbnailBasename = os.path.basename(thumbnailFilePath)
                       # Strip off thumbnail extension so we can match on the name.
                       thumbnailBasenameName, thumbnailBasenameext = os.path.splitext(thumbnailBasename)
                       if (thumbnailBasenameName in v['filename']):
                           thumbnailFilename = accessFileBasenameName + '.icon'
                           shutil.copy(thumbnailFilePath, os.path.join(outputItemDir, thumbnailFilename))

               # For each object file, output a .desc file. Currently, Archivematica does not
               # support child-level descriptions, so we can use the filename as the title if
               # there isn't a user-supplied csv or structMap to provide labels as per
               # https://www.archivematica.org/wiki/CONTENTdm_integration. Also note that we do
               # not add the non-DC metadata fields to child .desc files.
               dcMetadata = parseDmdSec(None, v['label'])
               # We don't want to include any values that are in nonDcMetadataForChildren,
               # (we just want the empty elements), so iterate through the copy we made at
               # the top of this function and zero all values in tis dictionary out.
               if nonDcMetadataForChildren != None:
                   for nonDcField, nonDcValue in nonDcMetadataForChildren.iteritems():
                       nonDcMetadataForChildren[nonDcField] = list()
               descFileContents = generateDescFile(dcMetadata, nonDcMetadataForChildren)
               descFilename = accessFileBasenameName + '.desc'
               descFile = open(os.path.join(outputItemDir, descFilename), "wb")
               descFile.write(descFileContents)
               descFile.close()

               # For each object file, add its .full file values. These entries do not
               # have anything in their <title> elements.
               fullFileContents += generateFullFileEntry('', accessFileBasenameName, accessFileBasenameExt)
               # For each object file, add its .cpd file values. 
               # @todo: We will need to account for hierarchical items here.
               cpdFileContent += "  <page>\n"
               cpdFileContent += "    <pagetitle>" + v['label'] + "</pagetitle>\n"
               cpdFileContent += "    <pagefile>" + v['filename'] + "</pagefile>\n"
               cpdFileContent += "    <pageptr>+</pageptr>\n"
               cpdFileContent += "  </page>\n"

    # Write out the index.full file. 
    fullFile = open(os.path.join(outputItemDir, 'index.full'), "wb")
    fullFile.write(fullFileContents)
    fullFile.close()

    # Write out the index.cpd file. We get the order of the items in the .cpd file
    # from the user-submitted structMap (if it is present) or the Archivematica
    # structMap (if no user-submitted structMap is present).
    cpdFileContent += '</cpd>'
    indexCpdFile = open(os.path.join(outputItemDir, 'index.cpd'), "wb")
    indexCpdFile.write(cpdFileContent)
    indexCpdFile.close()

    # Create a thumbnail for the parent item (index.icon), using the icon from the first item
    # in the METS file. parentThumbnailFilename
    shutil.copy(os.path.join(outputItemDir, parentThumbnailFilename), os.path.join(outputItemDir, 'index.icon'))

    # Write out the ready.txt file which contains the string '1'.
    readyFile = open(os.path.join(outputItemDir, 'ready.txt'), "wb")
    readyFile.write('1')
    readyFile.close()


# Generate a 'project client' package for a compound CONTENTdm item from the Archivematica DIP.
# This package will contain the object file and a delimited metadata file in a format suitable
# for importing into CONTENTdm using its Project Client.
def generateCompoundContentDMProjectClientPackage(dmdSecs, structMaps, dipUuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup, bulk):
    dmdSecPair = splitDmdSecs(dmdSecs)
    nonDcMetadata = dmdSecPair['nonDc']
    dcMetadata = dmdSecPair['dc']    
    collectionFieldInfo = getContentdmCollectionFieldInfo(args.contentdmServer, args.targetCollection)

    # Archivematica's stuctMap is always the first one; the user-submitted structMap
    # is always the second one. @todo: If the user-submitted structMap is present,
    # parse it for the SIP structure so we can use that structure in the CONTENTdm packages.
    # structMapDom =  metsDom.getElementsByTagName('structMap')[0]
    structMapDom = structMaps[0]
    structMapDict = parseStructMap(structMapDom, filesInObjectDirectoryForThisDmdSecGroup)
    
    # Each item needs to have its own directory under outputDipDir. To supply a unique UUID
    # for each compound item, we use the the first eight characters of the UUID of the first 
    # file in each compound item.
    if bulk:
        firstFilePath, firstFileFilename = os.path.split(filesInObjectDirectoryForThisDmdSecGroup[0])
        itemDirUuid = firstFileFilename[:8]
        outputItemDir = os.path.join(outputDipDir, itemDirUuid)
        os.mkdir(outputItemDir)
        # Copy the files into the outputItemDir, giving them names that reflect
        # the sort order expressed in their structMap.
        Orders = []
        for fptr, details in structMapDict.iteritems():
            Orders.append(details['order'])

        # Iterate through the list of order values and add the matching structMapDict entry
        # to the delimited file and copy the file into the scans directory.
        for order in sorted(Orders):
            for k, v in structMapDict.iteritems():
                if order == v['order']:
                    # Find the full path of the file identified in v['filename'].
                    for fullPath in filesInObjectDirectoryForThisDmdSecGroup:
                        if (v['filename'] in fullPath):
                            objectFilePath, objectFileFilename = os.path.split(v['filename'])
                            objectFileBaseFilename, objectFileExtension = os.path.splitext(objectFileFilename)
                            # We give the destination files a sortable numeric name (using their 'order'
                            # attribute from parseStructMap() so they sort properly in the Project Client.
                            shutil.copy(fullPath, os.path.join(outputItemDir, v['order'] + objectFileExtension))
  
    # I.e., single item in DIP. We take care of copying the files and assembling the
    # child-level metadata rows further down.
    else:
        scansDir = os.path.join(outputDipDir, 'scans')
        os.makedirs(scansDir)

    # Write out the metadata file, with the first row containing the field labels and the
    # second row containing the values. Both rows needs to be in the order expressed in
    # collectionFieldInfo['order']. For each item in collectionFieldInfo['order'],
    # query each mapping in collectionFieldInfo['mappings'] to find a matching 'nick';
    # if the nick is found, write the value in the dmdSec's element that matches the mapping's
    # key; if no matching mapping is found, write ''. The DIP filename (in this case, the file
    # variable defined above) needs to go in the last column.
    collectionFieldInfo = getContentdmCollectionFieldInfo(args.contentdmServer, args.targetCollection)
    delimHeaderRow = []
    delimItemValuesRow = []
    for field in collectionFieldInfo['order']:
        # Process the non-DC metadata, if there is any.
        if nonDcMetadata != None:
            for k, v in collectionFieldInfo['nonDcMappings'].iteritems():
                if field == v['nick']:
                   # Append the field name to the header row.
                   delimHeaderRow.append(v['name'])
                   # Append the element value to the values row.
                   if normalizeNonDcElementName(k) in nonDcMetadata:
                       # In CONTENTdm, repeated values are joined with a semicolon.
                       normalized_name = normalizeNonDcElementName(k)
                       joinedNonDcMetadataValues = '; '.join(nonDcMetadata[normalized_name])
                       # Rows can't contain new lines.
                       joinedNonDcMetadataValues = joinedNonDcMetadataValues.replace("\r","")
                       joinedNonDcMetadataValues = joinedNonDcMetadataValues.replace("\n","")
                       delimItemValuesRow.append(joinedNonDcMetadataValues)
                   # Append a placeholder to keep the row intact.
                   else:
                       delimItemValuesRow.append('')
        # I.e., there is no nonDcMetadata.
        else:
            for k, v in collectionFieldInfo['dcMappings'].iteritems():
                if field == v['nick']:
                    # Append the field name to the header row.
                    delimHeaderRow.append(v['name'])
                    # Append the element value to the values row.
                    if k in dcMetadata:
                        # In CONTENTdm, repeated values are joined with a semicolon.
                        joinedDcMetadataValues = '; '.join(dcMetadata[k])                   
                        # Rows can't contain new lines.
                        joinedDcMetadataValues = joinedDcMetadataValues.replace("\r","")
                        joinedDcMetadataValues = joinedDcMetadataValues.replace("\n","")
                        delimValuesRow.append(joinedDcMetadataValues)
                    # Append a placeholder to keep the row intact.
                    else:
                        delimValuesRow.append('')

    compoundTxtFilePath = os.path.join(outputDipDir, 'compound.txt')
    # Check to see if compound.txt already exists, and if it does, append delimValuesRow
    # to it.
    if os.path.exists(compoundTxtFilePath):
        delimitedFile = open(compoundTxtFilePath, "ab")
        writer = csv.writer(delimitedFile, delimiter='\t')
    # If it doesn't exist, write out the header row.
    else:
        delimitedFile = open(compoundTxtFilePath, "wb")
        writer = csv.writer(delimitedFile, delimiter='\t')
        # Write the header row. Headers for compound item Project Client packages have
        # the Directory name field in the first position.
        delimHeaderRow.insert(0, 'Directory name')
        writer.writerow(delimHeaderRow)

    # Prepend the item directory name to the row.
    delimItemValuesRow.insert(0, itemDirUuid)
    # Write the item-level metadata row.
    writer.writerow(delimItemValuesRow) 

    # Child-level metadata for compound items only applies to single-DIP items,
    # not bulk DIPs, since we're using the CONTENTdm 'object list' Project Client
    # method of importing (see http://www.contentdm.org/help6/objects/multiple4.asp).
    # Page labels need to be applied within the project client.
    if not bulk:
        # Determine the order in which we will add the child-level rows to the delimited file.
        Orders = []
        for fptr, details in structMapDict.iteritems():
            Orders.append(details['order'])

        # Iterate through the list of order values and add the matching structMapDict entry
        # to the delimited file (and copy the file into the scans directory).
        for order in sorted(Orders):
            for k, v in structMapDict.iteritems():
                if order == v['order']:
                    delimChildValuesRow = []
                    # Find the full path of the file identified in v['filename'].
                    for fullPath in filesInObjectDirectory:
                        if (v['filename'] in fullPath):
                            objectFilePath, objectFileFilename = os.path.split(v['filename'])
                            objectFileBaseFilename, objectFileExtension = os.path.splitext(objectFileFilename)
                            # We give the destination files a sortable numeric name (using their 'order'
                            # attribute from parseStructMap() so they sort properly in the Project Client.
                            shutil.copy(fullPath, os.path.join(outputItemDir, v['order'] + objectFileExtension))                            
                            
                    # Write the child-level metadata row. For single (non-bulk) DIPs, we use
                    # the delimited file format described at
                    # http://www.contentdm.org/help6/objects/adding3a.asp; for bulk DIPs, we
                    # use the 'object list' method described at
                    # http://www.contentdm.org/help6/objects/multiple4.asp. In this method, we
                    # should make sure the directory where the item's children are stored (identified
                    # in the input metadata.csv's 'parts' column) is used for the output delimited
                    # file's 'Directory Name' value; we can't use the item's title since it may
                    # contain characters that are illegal in directory names. This also means that
                    # we can just copy the child directory names into this field.
                    # @todo (applies to single, not bulk): For flat items with no child-level metadata, we are using the 
                    # label for the child as defined in structMapDict and the filename only.
                    # This means that we put the label in the position allocated for the dc.title element,
                    # and the filename in the last position. Everthing in between is ''. This will
                    # need to be made more functional for flat items with child-level metadata,
                    # and for hierarchical.
                    titlePosition = collectionFieldInfo['order'].index('title')
                    if titlePosition == 0:
                        delimChildValuesRow.append(v['label'])
                        for i in range(1, len(delimHeaderRow) - 1):
                            delimChildValuesRow.append('')
                    # Rows for compound itms must contain directory name in first position.
                    delimChildValuesRow.prepend(itemDirUuid)
                    writer.writerow(delimChildValuesRow)
               
    delimitedFile.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='restructure')
    parser.add_argument('--uuid', action="store", dest='uuid', metavar='UUID', help='SIP-UUID')
    parser.add_argument('--dipDir', action="store", dest='dipDir', metavar='dipDir', help='DIP Directory')
    parser.add_argument('--server', action="store", dest='contentdmServer', metavar='server', help='Target CONTENTdm server')
    parser.add_argument('--collection', action="store", dest='targetCollection',
                        metavar='targetCollection', help='Target CONTENTdm Collection')
    parser.add_argument('--ingestFormat', action="store", dest='ingestFormat', metavar='ingestFormat',
                        default='directupload', help='The format of the ingest package, either directupload or projectclient')
    parser.add_argument('--outputDir', action="store", dest='outputDir', metavar='outputDir',
                        help='The destination for the restructured DIPs')

    args = parser.parse_args()

    # Define the directory where DIPs are waiting to be processed.
    inputDipDir = args.dipDir
    
    # Use %watchDirectoryPath%uploadedDIPs as the output directory for the directupload and 
    # projectclient output. Also create a 'CONTENTdm' subdirectory for DIPs created by this microservice.
    outputDipDir = prepareOutputDir(args.outputDir, args.ingestFormat, args.uuid)

    # Perform some preliminary validation on the argument values.
    if not os.path.exists(inputDipDir):
        print "Can't find " + inputDipDir
        sys.exit(1)
    if args.ingestFormat not in ['directupload', 'projectclient']:
        print "IngestFormat must be either 'directupload' or 'projectclient'"
        sys.exit(1)

    # Read and parse the METS file. Assumes there is one METS file in the DIP directory,
    # which is true for both single-item transfers and bulk transfers.
    for infile in glob.glob(os.path.join(inputDipDir, "METS*.xml")):
        metsFile = infile
    metsDom = parse(metsFile)
    
    # Get the structMaps so we can pass them into the DIP creation functions.
    structMaps = metsDom.getElementsByTagName('structMap')

    itemCountType = getItemCountType(structMaps[0])

    # Populate lists of files in the DIP objects and thumbnails directories.
    filesInObjectDirectory = getObjectDirectoryFiles(os.path.join(inputDipDir, 'objects'))
    filesInThumbnailDirectory = glob.glob(os.path.join(inputDipDir, 'thumbnails', "*.jpg"))
    
    # Get the dmdSec nodes from the METS file.
    dmdSecs = metsDom.getElementsByTagName('dmdSec')
    numDmdSecs = len(dmdSecs)
    # Group the dmdSecs into item-specific pairs (for DC and OTHER) or if
    # OTHER is not present, just the DC.
    groupedDmdSecs = groupDmdSecs(dmdSecs)
    
    # Bulk DIP. Assumes that a single item (i.e. no bulk) will only have one
    # dmdSec, (i.e., not "dmdSec_1 dmdSec_2"). This is probably a safe assumption
    # because a single item's metadata would either come from a dublincore.xml
    # file or from the metadata entry form in the Dashboard. Only edge case
    # would be if the metadata was from a single-row metadata.csv file that had 
    # a combination of dcterms and custom metadata.
    if numDmdSecs > 1:
        # For simple items.  
        if itemCountType == 'simple':
            for dmdSecGroup in groupedDmdSecs:                
                filesInObjectDirectoryForThisDmdSecGroup = getFilesInObjectDirectoryForThisDmdSecGroup(dmdSecGroup, structMaps)
                if args.ingestFormat == 'directupload':
                    generateSimpleContentDMDirectUploadPackage(dmdSecGroup, structMaps, args.uuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup, filesInThumbnailDirectory)
                if args.ingestFormat == 'projectclient':
                    generateSimpleContentDMProjectClientPackage(dmdSecGroup, structMaps, args.uuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup)

        # For compound items.
        if itemCountType == 'compound': 
            for dmdSecGroup in groupedDmdSecs:
                filesInObjectDirectoryForThisDmdSecGroup = getFilesInObjectDirectoryForThisDmdSecGroup(dmdSecGroup, structMaps)
                if args.ingestFormat == 'directupload':
                    generateCompoundContentDMDirectUploadPackage(dmdSecGroup, structMaps,  args.uuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup, filesInThumbnailDirectory)
                if args.ingestFormat == 'projectclient':
                    generateCompoundContentDMProjectClientPackage(dmdSecGroup, structMaps, args.uuid, outputDipDir, filesInObjectDirectoryForThisDmdSecGroup, True)

    # 0 or 1 dmdSec (single-item DIP).
    else:
        # For simple items.
        if len(filesInObjectDirectory) <= 1 and args.ingestFormat == 'directupload':
            generateSimpleContentDMDirectUploadPackage(dmdSecs, structMaps, args.uuid, outputDipDir, filesInObjectDirectory, filesInThumbnailDirectory)
        if len(filesInObjectDirectory) <= 1 and args.ingestFormat == 'projectclient':
            generateSimpleContentDMProjectClientPackage(dmdSecs, structMaps, args.uuid, outputDipDir, filesInObjectDirectory)

        # For compound items.
        if len(filesInObjectDirectory) > 1 and args.ingestFormat == 'directupload':
            generateCompoundContentDMDirectUploadPackage(dmdSecs, structMaps, args.uuid, outputDipDir, filesInObjectDirectory, filesInThumbnailDirectory)
        if len(filesInObjectDirectory) > 1 and args.ingestFormat == 'projectclient':
            generateCompoundContentDMProjectClientPackage(dmdSecs, structMaps, args.uuid, outputDipDir, filesInObjectDirectory, False)
    
    if args.ingestFormat == 'projectclient':
        zipProjectClientOutput(outputDipDir, args.outputDir, args.uuid)
        # Delete the unzipped version of the DIP since we don't use it.
        shutil.rmtree(outputDipDir)
