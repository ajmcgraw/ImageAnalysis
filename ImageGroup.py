#!/usr/bin/python

import commands
import cv2
import fnmatch
import lxml.etree as ET
import math
from matplotlib import pyplot as plt
import numpy as np
import os.path
import subprocess
import sys

import PIL                      # test?

from getchar import find_getch
import Image
import ImageList
import Matcher
import Placer
import Render
import transformations


class ImageGroup():
    def __init__(self, max_features=100, detect_grid=8, match_ratio=0.5):
        cells = detect_grid * detect_grid
        self.max_features = int(max_features / cells)
        self.match_ratio = match_ratio
        self.detect_grid = detect_grid
        self.file_list = []
        self.image_list = []
        self.ac3d_steps = 8
        self.shutter_latency = 0.0
        self.group_roll_bias = 0.0
        self.group_pitch_bias = 0.0
        self.group_yaw_bias = 0.0
        self.group_alt_bias = 0.0
        self.k1 = 0.0
        self.k2 = 0.0
        self.m = Matcher.Matcher()
        self.placer = Placer.Placer()
        self.render = Render.Render()
        detectparams = dict(detector="sift", nfeatures=1000)
        #detectparams = dict(detector="surf", hessian_threshold=600)
        #detectparams = dict(detector="orb",  orb_max_features=2000,
        #                    dense_detect_grid=4)
        matcherparams = dict(matcher="flann", match_ratio=match_ratio)
        #matcherparams = dict(matcher="bruteforce", match_ratio=match_ratio)
        self.m.configure(detectparams, matcherparams)

    def setCameraParams(self, horiz_mm=23.5, vert_mm=15.7, focal_len_mm=30.0):
        self.horiz_mm = horiz_mm
        self.vert_mm = vert_mm
        self.focal_len_mm = focal_len_mm

    def setWorldParams(self, ground_alt_m=0.0):
        self.ground_alt_m = ground_alt_m

    def update_work_dir(self, source_dir="", work_dir="", 
                        width=684, height=456):
        self.source_dir=source_dir
        self.work_dir=work_dir
        # double check work dir exists and make it if not
        if not os.path.exists(self.work_dir):
            os.makedirs(self.work_dir)

        files = []
        for file in os.listdir(self.source_dir):
            if fnmatch.fnmatch(file, '*.jpg') or fnmatch.fnmatch(file, '*.JPG'):
                files.append(file)
        files.sort()

        for file in files:
            # create resized working copy if needed
            name_in = self.source_dir + "/" + file
            name_out = self.work_dir + "/" + file
            if not os.path.isfile(name_out):
                command = "convert -geometry 684x456 %s %s" \
                          % (name_in, name_out)
                print command
                commands.getstatusoutput( command )

    def load_project(self):
        project_file = self.work_dir + "/project.xml"
        if os.path.exists(project_file):
            print "Loading " + project_file
            try:
                xml = ET.parse(project_file)
                root = xml.getroot()
                self.shutter_latency = float(root.find('shutter-latency').text)
                self.group_roll_bias = float(root.find('roll-bias').text)
                self.group_pitch_bias = float(root.find('pitch-bias').text)
                self.group_yaw_bias = float(root.find('yaw-bias').text)
                self.group_alt_bias = float(root.find('altitude-bias').text)
            except:
                print project_file + ":\n" + "  load error: " \
                    + str(sys.exc_info()[1])

    def save_project(self):
        project_file = self.work_dir + "/project.xml"
        root = ET.Element('project')
        xml = ET.ElementTree(root)
        ET.SubElement(root, 'shutter-latency').text = "%.2f" % self.shutter_latency
        ET.SubElement(root, 'roll-bias').text = "%.2f" % self.group_roll_bias
        ET.SubElement(root, 'pitch-bias').text = "%.2f" % self.group_pitch_bias
        ET.SubElement(root, 'yaw-bias').text = "%.2f" % self.group_yaw_bias
        ET.SubElement(root, 'altitude-bias').text = "%.2f" % self.group_alt_bias
        # write xml file
        try:
            xml.write(project_file, encoding="us-ascii",
                      xml_declaration=False, pretty_print=True)
        except:
            print project_file + ": error saving file: " \
                + str(sys.exc_info()[1])

    def load(self):
        # load project wide values
        self.load_project()

        self.file_list = []
        for file in os.listdir(self.work_dir):
            if fnmatch.fnmatch(file, '*.jpg') or fnmatch.fnmatch(file, '*.JPG'):
                self.file_list.append(file)
        self.file_list.sort()
        for file_name in self.file_list:
            image = Image.Image(self.work_dir, file_name)
            if len(image.kp_list) == 0 or image.des_list == None:
                print "  detecting features and computing descriptors"
                full_image = image.load_full_image(self.source_dir)
                image.kp_list = self.m.denseDetect(full_image)
                image.kp_list, image.des_list \
                    = self.m.computeDescriptors(full_image, image.kp_list)
                # and because we've messed with keypoints and descriptors
                image.match_list = []
                image.save_keys()
                image.save_descriptors()
                #image.show_keypoints()
            self.image_list.append( image )
        # make sure our matcher gets a copy of the image list
        self.m.setImageList(self.image_list)
        self.placer.setImageList(self.image_list)
        self.render.setImageList(self.image_list)

    def genKeypointUsageMap(self):
        # make the keypoint usage map (used so we don't have to
        # project every keypoint every time)
        print "Building the keypoint usage map... ",
        for i1 in self.image_list:
            i1.kp_usage = np.zeros(len(i1.kp_list), np.bool_)
        for i, i1 in enumerate(self.image_list):
            for j, pairs in enumerate(i1.match_list):
                if len(pairs) == 0:
                    continue
                if i == j:
                    continue
                i2 = self.image_list[j]
                print "%s vs %s" % (i1.name, i2.name)
                for pair in pairs:
                    i1.kp_usage[pair[0]] = True
                    i2.kp_usage[pair[1]] = True
        print "done."

    def interpolateCamPositions(self, correlator, force=False, weight=True):
        # tag each image with the flight data parameters at the time
        # the image was taken
        for match in correlator.best_matchups:
            pict, trig = correlator.get_match(match)
            image = self.m.findImageByName(pict[2])
            if image != None:
                if force or (math.fabs(image.aircraft_lon) < 0.01 and math.fabs(image.aircraft_lat) < 0.01):
                    # only if we are forcing a new position
                    # calculation or the position is not already set
                    # from a save file.
                    t = trig[0] + self.shutter_latency
                    lon, lat, msl = correlator.get_position(t)
                    roll, pitch, yaw = correlator.get_attitude(t)
                    image.set_location( lon, lat, msl, roll, pitch, yaw )
                    if weight:
                        # presumes a pitch/roll distance of 10, 10 gives a
                        # zero weight
                        w = 1.0 - (roll*roll + pitch*pitch)/200.0
                        if w < 0.01:
                            w = 0.01
                        image.weight = w
                    else:
                        image.weight = 1.0
                    image.save_info()
                    #print "%s roll=%.1f pitch=%.1f weight=%.2f" % (image.name, roll, pitch, image.weight)

    def computeWeights(self, force=None):
        # tag each image with the flight data parameters at the time
        # the image was taken
        for image in self.image_list:
            roll = image.aircraft_roll + image.roll_bias
            pitch = image.aircraft_pitch + image.pitch_bias
            if force != None:
                image.weight = force
            else:
                # presumes a pitch/roll distance of 10, 10 gives a
                # zero weight
                w = 1.0 - (roll*roll + pitch*pitch)/200.0
                if w < 0.01:
                    w = 0.01
                    image.weight = w
            image.save_info()
            #print "%s roll=%.1f pitch=%.1f weight=%.2f" % (image.name, roll, pitch, image.weight)

    def computeConnections(self, force=None):
        for image in self.image_list:
            image.connections = 0
            for pairs in image.match_list:
                if len(pairs) >= self.m.min_pairs:
                    image.connections += 1
            image.save_info()
            print "%s connections: %d" % (image.name, image.connections)

    def computeRefLocation(self):
        # requires images to have their location computed/loaded
        lon_sum = 0.0
        lat_sum = 0.0
        for i in self.image_list:
            lon_sum += i.aircraft_lon
            lat_sum += i.aircraft_lat
        self.ref_lon = lon_sum / len(self.image_list)
        self.ref_lat = lat_sum / len(self.image_list)
        self.render.setRefCoord(self.ref_lon, self.ref_lat)
        print "Reference: lon = %.6f lat = %.6f" % (self.ref_lon, self.ref_lat)

    # undistort x, y using a simple radial lens distortion model.  (We
    # call the original image values the 'distorted' values.)  Input
    # x,y are expected to be normalize (0.0 - 1.0) in image pixel
    # space with 0.5 being the center of image (and hopefully the
    # center of distortion.)
    def doLensUndistort(self, aspect_ratio, xnorm, ynorm):
        xd = (xnorm * 2.0 - 1.0) * aspect_ratio
        yd = ynorm * 2.0 - 1.0
        r = math.sqrt(xd*xd + yd*yd)
        #print "ar=%.3f xd=%.3f yd=%.3f r=%.2f" % (aspect_ratio, xd, yd, r)
        factor = 1.0 + self.k1 * r*r + self.k2 * r*r*r*r
        xu = xd * factor
        yu = yd * factor
        xnorm_u = (xu / aspect_ratio + 1.0) / 2.0
        ynorm_u = (yu + 1.0) / 2.0
        #print "  (%.3f %.3f) -> (%.3f %.3f)" % (xnorm, ynorm, xnorm_u, ynorm_u)
        return xnorm_u, ynorm_u

    def projectImageKeypointsExternal(self, image, do_grid=False,
                                      yaw_bias=0.0, roll_bias=0.0,
                                      pitch_bias=0.0, alt_bias=0.0):
        Verbose = False

        prog = "/home/curt/Projects/UAS/ugear/build_linux-pc/utils/geo/geolocate"
        if image.img == None:
            image.load_image()
        h = image.fullh
        w = image.fullw
        ar = float(w)/float(h)  # aspect ratio
        lon = image.aircraft_lon
        lat = image.aircraft_lat
        msl = image.aircraft_msl + image.alt_bias + self.group_alt_bias + alt_bias
        roll = -(image.aircraft_roll + image.roll_bias + self.group_roll_bias + roll_bias)
        pitch = -(image.aircraft_pitch + image.pitch_bias + self.group_pitch_bias + pitch_bias)
        yaw = image.aircraft_yaw + image.yaw_bias + self.group_yaw_bias + yaw_bias
        yaw += 180.0        # camera is mounted backwards
        while yaw > 360.0:
            yaw -= 360.0
        while yaw < -360.0:
            yaw += 360.0
        #print "%s %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f" % (image.name, image.aircraft_roll, image.roll_bias, image.aircraft_pitch, image.pitch_bias, image.aircraft_yaw, image.yaw_bias, image.msl, image.alt_bias)
        if Verbose:
            for arg in [prog, str(lon), str(lat), str(msl), \
                        str(self.ground_alt_m), str(roll), str(pitch), \
                        str(yaw), str(self.horiz_mm), str(self.vert_mm), \
                        str(self.focal_len_mm), str(self.ref_lon), \
                        str(self.ref_lat)]:
                print arg,
            print
        process = subprocess.Popen([prog, str(lon), str(lat), str(msl), \
                                    str(self.ground_alt_m), str(roll), \
                                    str(pitch), \
                                    str(yaw), str(self.horiz_mm), \
                                    str(self.vert_mm), \
                                    str(self.focal_len_mm), \
                                    str(self.ref_lon), \
                                    str(self.ref_lat)], shell=False, \
                                   stdin=subprocess.PIPE, \
                                   stdout=subprocess.PIPE)

        # compute the keypoint locations in image space: [0.0,1.0][0.0,1.0]
        coords = ""
        for i, kp in enumerate(image.kp_list):
            if not image.kp_usage[i]:
                continue
            x = kp.pt[0]
            y = kp.pt[1]
            #print " project px = %.2f, %.2f" % (x, y)
            xnorm = x / float(w-1)
            ynorm = y / float(h-1)
            xnorm_u, ynorm_u = self.doLensUndistort(ar, xnorm, ynorm)
            coords += "%d %.5f %.5f\n" % (i, xnorm_u, ynorm_u)

        if True:
            # compute the corners (2x2 polygon grid) in image space
            dx = 1.0
            dy = 1.0
            ynorm = 0.0
            for j in xrange(2):
                xnorm = 0.0
                for i in xrange(2):
                    #print "cc %.2f %.2f" % (xnorm_u, ynorm_u)
                    xnorm_u, ynorm_u = self.doLensUndistort(ar, xnorm, ynorm)
                    coords += "cc %.3f %.3f\n" % (xnorm_u, ynorm_u)
                    xnorm += dx
                ynorm += dy

        if do_grid:
            # compute the ac3d polygon grid in image space
            dx = 1.0 / float(self.ac3d_steps)
            dy = 1.0 / float(self.ac3d_steps)
            ynorm = 0.0
            for j in xrange(self.ac3d_steps+1):
                xnorm = 0.0
                for i in xrange(self.ac3d_steps+1):
                    #print "cc %.2f %.2f" % (xnorm_u, ynorm_u)
                    xnorm_u, ynorm_u = self.doLensUndistort(ar, xnorm, ynorm)
                    coords += "gr %.3f %.3f\n" % (xnorm_u, ynorm_u)
                    xnorm += dx
                ynorm += dy

        # call the external project code
        result = process.communicate( coords )

        coord_list = [None] * len(image.kp_list)
        corner_list = []
        grid_list = []
        if Verbose:
            print image.name
        #f = open( 'junk', 'w' )
        for line in str(result[0]).split("\n"):
            #print "line = " + line
            tokens = line.split()
            if len(tokens) != 5 or tokens[0] != "result:":
                continue
            id = tokens[1]
            x = float(tokens[2]) + image.x_bias
            y = float(tokens[3]) + image.y_bias
            z = float(tokens[4])
            #print [ x, y, z ]
            if id == 'cc':
                corner_list.append( [x, y] )
            elif id == 'gr':
                grid_list.append( [x, y] )
            else:
                # print " project map = %.2f, %.2f" % (x, y)
                coord_list[int(id)] = [x, y]
            #f.write("%.2f\t%.2f\n" % (x, y))
        #f.close()
        return coord_list, corner_list, grid_list

    def projectPoint1(self, image, q, pt, z_m, horiz_mm, vert_mm, focal_len_mm):
        h = image.fullh
        w = image.fullw
        ar = float(w)/float(h)  # aspect ratio

        # normalized pixel coordinates to [0.0, 1.0]
        xnorm = pt[0] / float(w-1)
        ynorm = pt[1] / float(h-1)

        # lens un-distortion
        xnorm_u, ynorm_u = self.doLensUndistort(ar, xnorm, ynorm)

        # compute pixel coordinate in sensor coordinate space (mm
        # units) with (0mm, 0mm) being the center of the image.
        x_mm = (xnorm_u * 2.0 - 1.0) * (horiz_mm * 0.5)
        y_mm = -1.0 * (ynorm_u * 2.0 - 1.0) * (vert_mm * 0.5)
        camvec = [y_mm, x_mm, focal_len_mm]
        camvec = transformations.unit_vector(camvec) # normalize
        #print "%.3f %.3f %.3f" % (camvec[0], camvec[1], camvec[2])

        # transform camera vector (in body reference frame) to ned
        # reference frame
        ned = transformations.quaternion_backTransform(q, camvec)
        #print "%.3f %.3f %.3f" % (ned[0], ned[1], ned[2])
        
        # solve projection
        if ned[2] <= 0.0:
            # no interseciton
            return None
        factor = z_m / ned[2]
        x_proj = ned[0] * factor
        y_proj = ned[1] * factor
        #print "proj dist = %.2f" % math.sqrt(x_proj*x_proj + y_proj*y_proj)
        return [x_proj, y_proj]

    def projectImageKeypointsNative1(self, image, do_grid=False,
                                    yaw_bias=0.0, roll_bias=0.0, pitch_bias=0.0,
                                    alt_bias=0.0):
        Verbose = False

        if image.img == None:
            image.load_image()
        h = image.fullh
        w = image.fullw
        ar = float(w)/float(h)  # aspect ratio
        lon = image.aircraft_lon
        lat = image.aircraft_lat
        msl = image.aircraft_msl + image.alt_bias + self.group_alt_bias + alt_bias
        roll = -(image.aircraft_roll + image.roll_bias + self.group_roll_bias + roll_bias)
        pitch = -(image.aircraft_pitch + image.pitch_bias + self.group_pitch_bias + pitch_bias)
        yaw = image.aircraft_yaw + image.yaw_bias + self.group_yaw_bias + yaw_bias
        yaw += 180.0        # camera is mounted backwards
        while yaw > 360.0:
            yaw -= 360.0
        while yaw < -360.0:
            yaw += 360.0
        #print "%s %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f" % (image.name, image.aircraft_roll, image.roll_bias, image.aircraft_pitch, image.pitch_bias, image.aircraft_yaw, image.yaw_bias, image.msl, image.alt_bias)

        if False:
            prog = "/home/curt/Projects/UAS/ugear/build_linux-pc/utils/geo/geolocate"
            for arg in [prog, str(lon), str(lat), str(msl), \
                        str(self.ground_alt_m), str(roll), str(pitch), \
                        str(yaw), str(self.horiz_mm), str(self.vert_mm), \
                        str(self.focal_len_mm), str(self.ref_lon), \
                        str(self.ref_lat)]:
                print arg,
            print

        deg2rad = math.pi / 180.0
        #roll = 0.0; pitch = 0.0; yaw = 45.0
        #print "(1) total yaw = %.2f pitch = %.2f roll = %.2f" % ( yaw, pitch, roll )
        q = transformations.quaternion_from_euler(yaw*deg2rad,
                                                  pitch*deg2rad,
                                                  roll*deg2rad,
                                                  'rzyx')
        (x_m, y_m) = ImageList.wgs842cart(lon, lat, self.ref_lon, self.ref_lat)
        z_m = msl - self.ground_alt_m
        #print "ref offset = %.2f %.2f" % (x_m, y_m)

        coord_list = [None] * len(image.kp_list)
        corner_list = []
        grid_list = []

        # project the paired keypoints into world space
        for i, kp in enumerate(image.kp_list):
            if not image.kp_usage[i]:
                continue
            proj = self.projectPoint1(image, q, kp.pt, z_m,
                                     self.horiz_mm, self.vert_mm,
                                     self.focal_len_mm)
            coord_list[i] = [proj[1] + image.x_bias + x_m,
                             proj[0] + image.y_bias + y_m]

        # compute the corners (2x2 polygon grid) in image space
        dx = image.fullw - 1
        dy = image.fullh - 1
        y = 0.0
        for j in xrange(2):
            x = 0.0
            for i in xrange(2):
                #print "corner %.2f %.2f" % (x, y)
                proj = self.projectPoint1(image, q, [x, y], z_m,
                                         self.horiz_mm, self.vert_mm,
                                         self.focal_len_mm)
                corner_list.append( [proj[1] + image.x_bias + x_m,
                                     proj[0] + image.y_bias + y_m] )
                x += dx
            y += dy

        # compute the ac3d polygon grid in image space
        dx = image.fullw / float(self.ac3d_steps)
        dy = image.fullh / float(self.ac3d_steps)
        y = 0.0
        for j in xrange(self.ac3d_steps+1):
            x = 0.0
            for i in xrange(self.ac3d_steps+1):
                #print "grid %.2f %.2f" % (xnorm_u, ynorm_u)
                proj = self.projectPoint1(image, q, [x, y], z_m,
                                         self.horiz_mm, self.vert_mm,
                                         self.focal_len_mm)
                grid_list.append( [proj[1] + image.x_bias + x_m,
                                   proj[0] + image.y_bias + y_m] )
                x += dx
            y += dy

        return coord_list, corner_list, grid_list

    def projectPoint2(self, image, q, pt, z_m, horiz_mm, vert_mm, focal_len_mm):
        h = image.fullh
        w = image.fullw
        ar = float(w)/float(h)  # aspect ratio

        # normalized pixel coordinates to [0.0, 1.0]
        xnorm = pt[0] / float(w-1)
        ynorm = pt[1] / float(h-1)

        # lens un-distortion
        xnorm_u, ynorm_u = self.doLensUndistort(ar, xnorm, ynorm)

        # compute pixel coordinate in sensor coordinate space (mm
        # units) with (0mm, 0mm) being the center of the image.
        x_mm = (xnorm_u * 2.0 - 1.0) * (horiz_mm * 0.5)
        y_mm = -1.0 * (ynorm_u * 2.0 - 1.0) * (vert_mm * 0.5)

        # the forward vector (out the nose when the aircraft is
        # straight, level, and flying north) is (x=1.0, y=0.0, z=0.0).
        # This vector will get projected to the camera center point,
        # thus we have to remap the axes.
        #camvec = [y_mm, x_mm, focal_len_mm]
        camvec = [focal_len_mm, x_mm, y_mm]
        camvec = transformations.unit_vector(camvec) # normalize
        #print "%.3f %.3f %.3f" % (camvec[0], camvec[1], camvec[2])

        # transform camera vector (in body reference frame) to ned
        # reference frame
        ned = transformations.quaternion_backTransform(q, camvec)
        #print "%.3f %.3f %.3f" % (ned[0], ned[1], ned[2])
        
        # solve projection
        if ned[2] >= 0.0:
            # no interseciton
            return None
        factor = z_m / -ned[2]
        x_proj = ned[0] * factor
        y_proj = ned[1] * factor
        #print "proj dist = %.2f" % math.sqrt(x_proj*x_proj + y_proj*y_proj)
        return [x_proj, y_proj]

    def projectImageKeypointsNative2(self, image, do_grid=False,
                                    yaw_bias=0.0, roll_bias=0.0, pitch_bias=0.0,
                                    alt_bias=0.0):
        if image.img == None:
            image.load_image()
        h = image.fullh
        w = image.fullw
        ar = float(w)/float(h)  # aspect ratio
        lon = image.aircraft_lon
        lat = image.aircraft_lat
        msl = image.aircraft_msl + image.alt_bias + self.group_alt_bias + alt_bias
        body_roll = -(image.aircraft_roll + image.roll_bias + roll_bias)
        body_pitch = -(image.aircraft_pitch + image.pitch_bias + pitch_bias)
        body_yaw = image.aircraft_yaw + image.yaw_bias + yaw_bias
        #print "%s %.2f %.2f %.2f %.2f %.2f %.2f %.2f %.2f" % (image.name, image.aircraft_roll, image.roll_bias, image.aircraft_pitch, image.pitch_bias, image.aircraft_yaw, image.yaw_bias, image.msl, image.alt_bias)

        deg2rad = math.pi / 180.0
        #body_yaw = 45.0; body_pitch = 0.0; body_roll = 10.0
        #print "body yaw = %.2f pitch = %.2f roll = %.2f" % ( body_yaw, body_pitch, body_roll )
        ned2body = transformations.quaternion_from_euler(body_yaw * deg2rad,
                                                         body_pitch * deg2rad,
                                                         body_roll * deg2rad,
                                                         'rzyx')
        yaw_cam = 180.0 + self.group_yaw_bias
        pitch_cam = -(-90.0 + self.group_pitch_bias)
        roll_cam = -(self.group_roll_bias)
        #yaw_cam = 0.0; pitch_cam = 10.0; roll_cam = 0.0
        #print "cam yaw = %.2f pitch = %.2f roll = %.2f" % ( yaw_cam, pitch_cam, roll_cam )
        body2cam = transformations.quaternion_from_euler(yaw_cam * deg2rad,
                                                         pitch_cam * deg2rad,
                                                         roll_cam * deg2rad,
                                                         'rzyx')
        q = transformations.quaternion_multiply(ned2body, body2cam)
        #q = transformations.quaternion_multiply(body2cam, ned2body)
        (yaw, pitch, roll) = transformations.euler_from_quaternion(q, 'rzyx')
        #print "total yaw = %.2f pitch = %.2f roll = %.2f" % ( yaw/deg2rad, pitch/deg2rad, roll/deg2rad )
        (x_m, y_m) = ImageList.wgs842cart(lon, lat, self.ref_lon, self.ref_lat)
        z_m = msl - self.ground_alt_m
        #print "ref offset = %.2f %.2f" % (x_m, y_m)

        # save the camera pose
        image.camera_yaw = yaw / deg2rad
        image.camera_pitch = pitch / deg2rad
        image.camera_roll = roll / deg2rad
        image.camera_x = x_m
        image.camera_y = y_m
        image.camera_z = z_m
        image.save_info()

        coord_list = [None] * len(image.kp_list)
        corner_list = []
        grid_list = []

        # project the paired keypoints into world space
        for i, kp in enumerate(image.kp_list):
            if not image.kp_usage[i]:
                continue
            proj = self.projectPoint2(image, q, kp.pt, z_m,
                                     self.horiz_mm, self.vert_mm,
                                     self.focal_len_mm)
            coord_list[i] = [proj[1] + image.x_bias + x_m,
                             proj[0] + image.y_bias + y_m]

        # compute the corners (2x2 polygon grid) in image space
        dx = image.fullw - 1
        dy = image.fullh - 1
        y = 0.0
        for j in xrange(2):
            x = 0.0
            for i in xrange(2):
                #print "corner %.2f %.2f" % (x, y)
                proj = self.projectPoint2(image, q, [x, y], z_m,
                                         self.horiz_mm, self.vert_mm,
                                         self.focal_len_mm)
                corner_list.append( [proj[1] + image.x_bias + x_m,
                                     proj[0] + image.y_bias + y_m] )
                x += dx
            y += dy

        # compute the ac3d polygon grid in image space
        dx = image.fullw / float(self.ac3d_steps)
        dy = image.fullh / float(self.ac3d_steps)
        y = 0.0
        for j in xrange(self.ac3d_steps+1):
            x = 0.0
            for i in xrange(self.ac3d_steps+1):
                #print "grid %.2f %.2f" % (xnorm_u, ynorm_u)
                proj = self.projectPoint2(image, q, [x, y], z_m,
                                         self.horiz_mm, self.vert_mm,
                                         self.focal_len_mm)
                grid_list.append( [proj[1] + image.x_bias + x_m,
                                   proj[0] + image.y_bias + y_m] )
                x += dx
            y += dy

        return coord_list, corner_list, grid_list

    def projectKeypoints(self, do_grid=False):
        for image in self.image_list:
            coord_list, corner_list, grid_list \
                = self.projectImageKeypointsNative2(image, do_grid)
            image.coord_list = coord_list
            image.corner_list = corner_list
            if do_grid:
                image.grid_list = grid_list
            # test
            # coord_list, corner_list, grid_list \
            #    = self.projectImageKeypointsNative2(image, do_grid)
            #print "orig corners = %s" % str(image.corner_list)
            #print "new corners = %s" % str(corner_list)

    def findImageRotate(self, i1, gain):
        #self.findImageAffine(i1) # temp test
        error_sum = 0.0
        weight_sum = i1.weight  # give ourselves an appropriate weight
        for i, match in enumerate(i1.match_list):
            if len(match) >= self.m.min_pairs:
                i2 = self.image_list[i]
                print "Rotating %s vs %s" % (i1.name, i2.name)
                for pair in match:
                    # + 180 (camera is mounted backwards)
                    y1 = i1.yaw + i1.rotate + 180.0
                    y2 = i2.yaw + i2.rotate + 180.0
                    dy = y2 - y1
                    while dy < -180.0:
                        dy += 360.0;
                    while dy > 180.0:
                        dy -= 360.0

                    # angle is in opposite direction from yaw
                    #a1 = i1.yaw + i1.rotate + 180 + i1.kp_list[pair[0]].angle
                    #a2 = i2.yaw + i2.rotate + 180 + i2.kp_list[pair[1]].angle
                    a1 = i1.kp_list[pair[0]].angle
                    a2 = i2.kp_list[pair[1]].angle
                    da = a1 - a2
                    while da < -180.0:
                        da += 360.0;
                    while da > 180.0:
                        da -= 360.0
                    print "yaw diff = %.1f  angle diff = %.1f" % (dy, da)

                    error = dy - da
                    while error < -180.0:
                        error += 360.0;
                    while error > 180.0:
                        error -= 360.0

                    error_sum += error * i2.weight
                    weight_sum += i2.weight
                    print str(pair)
                    print " i1: %.1f %.3f %.1f" % (i1.yaw, i1.kp_list[pair[0]].angle, a1)
                    print " i2: %.1f %.3f %.1f" % (i2.yaw, i2.kp_list[pair[1]].angle, a2)
                    print " error: %.1f  weight: %.2f" % (error, i2.weight)
                    print
                #self.showMatch(i1, i2, match)
        update = 0.0
        if weight_sum > 0.0:
            update = error_sum / weight_sum
        i1.rotate += update * gain
        print "Rotate %s delta=%.2f = %.2f" % (i1.name,  update, i1.rotate)

    def rotateImages(self, gain=0.10):
        for image in self.image_list:
            self.findImageRotate(image, gain)
        for image in self.image_list:
            print "%s: yaw error = %.2f" % (image.name, image.rotate)
                    
    def findImagePairShift(self, i1, i2, match):
        xerror_sum = 0.0
        yerror_sum = 0.0
        for pair in match:
            c1 = i1.coord_list[pair[0]]
            c2 = i2.coord_list[pair[1]]
            dx = c2[0] - c1[0]
            dy = c2[1] - c1[1]
            xerror_sum += dx
            yerror_sum += dy
        # divide by pairs + 1 gives some weight to our own position
        # (i.e. a zero rotate)
        xshift = xerror_sum / len(match)
        yshift = yerror_sum / len(match)
        #print " %s -> %s = (%.2f %.2f)" % (i1.name, i2.name, xshift, yshift)
        return (xshift, yshift)

    def findImageShift(self, i1, gain=0.10, placing=False):
        xerror_sum = 0.0
        yerror_sum = 0.0
        weight_sum = i1.weight  # give ourselves an appropriate weight
        for i, match in enumerate(i1.match_list):
            if len(match) < self.m.min_pairs:
                continue
            i2 = self.image_list[i]
            #if not i2.placed:
            #    continue
            (xerror, yerror) = self.findImagePairShift( i1, i2, match )
            xerror_sum += xerror * i2.weight
            yerror_sum += yerror * i2.weight
            weight_sum += i2.weight
        xshift = xerror_sum / weight_sum
        yshift = yerror_sum / weight_sum
        print "Shift %s -> (%.2f %.2f)" % (i1.name, xshift, yshift)
        #print " %s bias before (%.2f %.2f)" % (i1.name, i1.x_bias, i1.y_bias)
        i1.x_bias += xshift * gain
        i1.y_bias += yshift * gain
        #print " %s bias after (%.2f %.2f)" % (i1.name, i1.x_bias, i1.y_bias)
        i1.save_info()

    def shiftImages(self, gain=0.10):
        for image in self.image_list:
            self.findImageShift(image, gain)

    # method="average": return the weighted average of the errors.
    # method="stddev": return the weighted average of the stddev of the errors.
    # method="max": return the max error of the subcomponents.
    def groupError(self, method="average"):
        #print "compute group error, method = %s" % method
        if len(self.image_list):
            error_sum = 0.0
            weight_sum = 0.0
            for i, image in enumerate(self.image_list):
                e = 0.0
                e = self.m.imageError(i, method=method)
                #print "%s error = %.2f" % (image.name, e)
                error_sum += e*e * image.weight
                weight_sum += image.weight
            return math.sqrt(error_sum / weight_sum)
        else:
            return 0.0

    # zero all biases (if we want to start over with a from scratch fit)
    def zeroImageBiases(self):
        for image in self.image_list:
            image.yaw_bias = 0.0
            image.roll_bias = 0.0
            image.pitch_bias = 0.0
            image.alt_bias = 0.0
            image.x_bias = 0.0
            image.y_bias = 0.0
            image.save_info()

    # try to fit individual images by manipulating various parameters
    # and testing to see if that produces a better fit metric
    def estimateParameter(self, i, ground_alt_m, method,
                          param="", start_value=0.0, step_size=1.0,
                          refinements=3):
        image = self.image_list[i]

        #print "Estimate %s for %s" % (param, image.name)
        var = False
        if method == "average":
            var = False
        elif method == "stddev":
            var = True
        for k in xrange(refinements):
            best_error = self.m.imageError(i, method=method)
            best_value = start_value
            test_value = start_value - 5*step_size
            #print "start value = %.2f error = %.1f" % (best_value, best_error)

            while test_value <= start_value + 5*step_size + (step_size*0.1):
                coord_list = []
                corner_list = []
                grid_list = []
                if param == "yaw":
                    coord_list, corner_list, grid_list \
                        = self.projectImageKeypointsNative2(image,
                                                     yaw_bias=test_value)
                elif param == "roll":
                    coord_list, corner_list, grid_list \
                        = self.projectImageKeypointsNative2(image,
                                                     roll_bias=test_value)
                elif param == "pitch":
                    coord_list, corner_list, grid_list \
                        = self.projectImageKeypointsNative2(image,
                                                     pitch_bias=test_value)
                elif param == "altitude":
                    coord_list, corner_list, grid_list \
                        = self.projectImageKeypointsNative2(image,
                                                     alt_bias=test_value)
                error = self.m.imageError(i, alt_coord_list=coord_list,
                                          method=method)
                #print "Test %s error @ %.2f = %.2f" % ( param, test_value, error )
                if error < best_error:
                    best_error = error
                    best_value = test_value
                    #print " better value = %.2f, error = %.1f" % (best_value, best_error)
                test_value += step_size
            # update values for next iteration
            start_value = best_value
            step_size /= 5.0
        return best_value, best_error

    # try to fit individual images by manipulating various parameters
    # and testing to see if that produces a better fit metric
    def fitImage(self, i, method, gain):
        # parameters to manipulate = yaw, roll, pitch
        yaw_step = 2.0
        roll_step = 1.0
        pitch_step = 1.0
        refinements = 4

        image = self.image_list[i]

        # start values should be zero because previous values are
        # already included so we are computing a new offset from the
        # past solution.
        yaw, e = self.estimateParameter(i, self.ground_alt_m, method,
                                        "yaw", start_value=0.0,
                                        step_size=1.0, refinements=refinements)
        roll, e = self.estimateParameter(i, self.ground_alt_m, method,
                                         "roll", start_value=0.0,
                                         step_size=1.0, refinements=refinements)
        pitch, e = self.estimateParameter(i, self.ground_alt_m, method,
                                          "pitch", start_value=0.0,
                                          step_size=1.0,
                                          refinements=refinements)
        alt, e = self.estimateParameter(i, self.ground_alt_m, method,
                                        "altitude", start_value=0.0,
                                        step_size=2.0, refinements=refinements)
        image.yaw_bias += yaw*gain
        image.roll_bias += roll*gain
        image.pitch_bias += pitch*gain
        image.alt_bias += alt*gain
        coord_list = []
        corner_list = []
        grid_list = []
        # but don't save the results so we don't bias future elements
        # with moving previous elements
        coord_list, corner_list, grid_list = self.projectImageKeypointsNative2(image)
        error = self.m.imageError(i, alt_coord_list=coord_list, method=method)
        if method == "average":
            image.error = error
        elif method == "stddev":
            image.stddev = error
        print "Fit %s (%s) is %.2f %.2f %.2f %.2f (avg=%.3f stddev=%.3f)" \
            % (image.name, method,
               image.yaw_bias, image.roll_bias, image.pitch_bias,
               image.alt_bias, image.error, image.stddev)
        image.save_info()

    # try to fit individual images by manipulating various parameters
    # and testing to see if that produces a better fit metric
    def fitImageAffine3d(self, i, method, gain):
        i1 = self.image_list[i]
        angles_sum = [0.0, 0.0, 0.0]
        weight_sum = i1.weight
        for j, pairs in enumerate(i1.match_list):
            if len(pairs) < self.m.min_pairs:
                continue
            i2 = self.image_list[j]
            src = [[], [], []]
            dst = [[], [], []]
            for pair in pairs:
                c1 = i1.coord_list[pair[0]]
                c2 = i2.coord_list[pair[1]] 
                src[0].append(c1[0])
                src[1].append(c1[1])
                src[2].append(0.0)
                dst[0].append(c2[0])
                dst[1].append(c2[1])
                dst[2].append(0.0)
            Aff3D = transformations.superimposition_matrix(src, dst)
            scale, shear, angles, trans, persp = transformations.decompose_matrix(Aff3D)
            print "%s vs. %s" % (i1.name, i2.name)
            #print "  scale = %s" % str(scale)
            #print "  shear = %s" % str(shear)
            print "  angles = %s" % str(angles)
            #print "  trans = %s" % str(trans)
            #print "  persp = %s" % str(persp)

            # this is all based around the assumption that our angle
            # differences area relatively small
            for k in range(3):
                a = angles[k]
                if a < -180.0:
                    a += 360.0
                if a > 180.0:
                    a -= 360.0
                angles_sum[k] += a
            weight_sum += i2.weight
        angles = [ angles_sum[0] / weight_sum,
                   angles_sum[1] / weight_sum,
                   angles_sum[2] / weight_sum ]
        print "average angles = %s" % str(angles)

        rad2deg = 180.0 / math.pi
        i1.roll_bias += angles[0] * rad2deg * gain
        i1.pitch_bias += angles[1] * rad2deg * gain
        i1.yaw_bias += angles[2] * rad2deg * gain

        coord_list = []
        corner_list = []
        grid_list = []
        # but don't save the results so we don't bias future elements
        # with moving previous elements
        coord_list, corner_list, grid_list = self.projectImageKeypointsNative2(i1)
        error = self.m.imageError(i, alt_coord_list=coord_list, method="average")
        stddev = self.m.imageError(i, alt_coord_list=coord_list, method="stddev")
        print "average error = %.3f" % error
        print "average stddev = %.3f" % stddev
        i1.save_info()

    def fitImagesIndividually(self, method, gain):
        for i, image in enumerate(self.image_list):
            self.fitImage(i, method, gain)
            #self.fitImageAffine3d(i, method, gain)

    def geotag_pictures( self, correlator, dir = ".", geotag_dir = "." ):
        ground_sum = 0.0
        ground_count = 0
        print "master_time_offset = " + str(correlator.master_time_offset)
        for match in correlator.best_matchups:
            pict, trig = correlator.get_match(match)
            trig_time = trig[0] + correlator.master_time_offset
            pict_time = pict[0]

            time_diff = trig_time - pict_time
            #print str(match[0]) + " <=> " + str(match[1])
            #print str(pict_time) + " <=> " + str(trig_time)
            print pict[2] + " -> " + str(trig[2]) + ", " + str(trig[3]) + ": " + str(trig[4]) + " (" + str(time_diff) + ")"
            agl_ft = trig[4]
            lon_deg, lat_deg, msl = correlator.get_position( trig[0] )
            msl_ft = msl / 0.3048
            ground_sum += (msl_ft - agl_ft)
            ground_count += 1
            ground_agl_ft = ground_sum / ground_count
            print "  MSL: " + str( msl_ft ) + " AGL: " + str(agl_ft) + " Ground: " + str(ground_agl_ft)

            # double check geotag dir exists and make it if not
            if not os.path.exists(geotag_dir):
                os.makedirs(geotag_dir)

            # update a resized copy if needed
            name_in = dir + "/" + pict[2]
            name_out = geotag_dir + "/" + pict[2]
            if not os.path.isfile( name_out ):
                command = 'convert -geometry 684x456 ' + name_in + ' ' + name_out
                #command = 'convert -geometry 512x512\! ' + name_in + ' ' + name_out
                print command
                commands.getstatusoutput( command )

            # update the gps meta data
            exif = pyexiv2.ImageMetadata(name_out)
            exif.read()
            #exif.set_gps_info(lat_deg, lon_deg, (msl_ft*0.3048))
            altitude = msl_ft*0.3048
            GPS = 'Exif.GPSInfo.GPS'
            exif[GPS + 'AltitudeRef']  = '0' if altitude >= 0 else '1'
            exif[GPS + 'Altitude']     = Fraction(altitude)
            exif[GPS + 'Latitude']     = decimal_to_dms(lat_deg)
            exif[GPS + 'LatitudeRef']  = 'N' if lat_deg >= 0 else 'S'
            exif[GPS + 'Longitude']    = decimal_to_dms(lon_deg)
            exif[GPS + 'LongitudeRef'] = 'E' if lon_deg >= 0 else 'W'
            exif[GPS + 'MapDatum']     = 'WGS-84'
            exif.write()

    def fixup_timestamps( self, correlator, camera_time_error, geotag_dir = "." ):
        for match in correlator.best_matchups:
            pict, trig = correlator.get_match(match)
            unixtime = pict[0]
            name = geotag_dir + "/" + pict[2]

            unixtime += camera_time_error
            newdatetime = datetime.datetime.utcfromtimestamp(round(unixtime)).strftime('%Y:%m:%d %H:%M:%S')
            exif = pyexiv2.ImageMetadata(name)
            exif.read()
            print "old: " + str(exif['Exif.Image.DateTime']) + "  new: " + newdatetime
            exif['Exif.Image.DateTime'] = newdatetime
            exif.write()

    def generate_camera_location_report(self):
        for image in self.image_list:
            print "%s\t%.10f\t%.10f\t%.2f" \
                % (image.name, image.aircraft_lon, image.aircraft_lat,
                   image.aircraft_msl)

    def generate_ac3d(self, correlator, ref_image = False, base_name="quick", version=None ):
        max_roll = 30.0
        max_pitch = 30.0
        min_agl = 50.0
        min_time = 0.0 # the further into the flight hopefully the better the filter convergence

        ref_lon = None
        ref_lat = None

        # count matching images (starting with 1 to include the reference image)
        match_count = 0
        if ref_image:
            match_count += 1
        for image in self.image_list:
            msl = image.aircraft_msl
            roll = -image.aircraft_roll
            pitch = -image.aircraft_pitch
            agl = msl - self.ground_alt_m
            if image.has_matches and math.fabs(roll) <= max_roll and math.fabs(pitch) <= max_pitch and agl >= min_agl:
                match_count += 1

        # write AC3D header
        name = self.work_dir
        name += "/"
        name += base_name
        if version:
            name += ("-%02d" % version)
        name += ".ac"
        f = open( name, "w" )
        f.write("AC3Db\n")
        f.write("MATERIAL \"\" rgb 1 1 1  amb 0.6 0.6 0.6  emis 0 0 0  spec 0.5 0.5 0.5  shi 10  trans 0.4\n")
        f.write("OBJECT world\n")
        f.write("rot 1.0 0.0 0.0  0.0 0.0 1.0 0.0 1.0 0.0")
        f.write("kids " + str(match_count) + "\n")

        for image in self.image_list:
            msl = image.aircraft_msl
            roll = -image.aircraft_roll
            pitch = -image.aircraft_pitch
            agl = msl - self.ground_alt_m
            if not image.has_matches or math.fabs(roll) > max_roll or math.fabs(pitch) > max_pitch or agl < min_agl:
                continue

            # compute a priority function (higher priority tiles are raised up)
            priority = (1.0-image.weight) - agl/400.0

            #ll = list(image.grid_list[0])
            #ll.append( -priority )
            #lr = list(image.grid_list[1])
            #lr.append( -priority )
            #ur = list(image.grid_list[2])
            #ur.append( -priority )
            #ul = list(image.grid_list[3])
            #ul.append( -priority )

            f.write("OBJECT poly\n")
            f.write("name \"rect\"\n")
            f.write("texture \"./" + image.name + "\"\n")
            f.write("loc 0 0 0\n")

            f.write("numvert %d\n" % ((self.ac3d_steps+1) * (self.ac3d_steps+1)))
            # output the ac3d polygon grid (note the grid list is in
            # this specific order because that is how we generated it
            # earlier
            pos = 0
            for j in xrange(self.ac3d_steps+1):
                for i in xrange(self.ac3d_steps+1):
                    v = image.grid_list[pos]
                    f.write( "%.3f %.3f %.3f\n" % (v[0], v[1], priority) )
                    pos += 1
  
            f.write("numsurf %d\n" % (self.ac3d_steps * self.ac3d_steps))
            dx = 1.0 / float(self.ac3d_steps)
            dy = 1.0 / float(self.ac3d_steps)
            y = 1.0
            for j in xrange(self.ac3d_steps):
                x = 0.0
                for i in xrange(self.ac3d_steps):
                    c = (j * (self.ac3d_steps+1)) + i
                    d = ((j+1) * (self.ac3d_steps+1)) + i
                    f.write("SURF 0x20\n")
                    f.write("mat 0\n")
                    f.write("refs 4\n")
                    f.write("%d %.3f %.3f\n" % (d, x, y-dy))
                    f.write("%d %.3f %.3f\n" % (d+1, x+dx, y-dy))
                    f.write("%d %.3f %.3f\n" % (c+1, x+dx, y))
                    f.write("%d %.3f %.3f\n" % (c, x, y))
                    x += dx
                y -= dy
            f.write("kids 0\n")

        if ref_image:
            # reference poly
            f.write("OBJECT poly\n")
            f.write("name \"rect\"\n")
            f.write("texture \"Reference/3drc.png\"\n")
            f.write("loc 0 0 0\n")
            f.write("numvert 4\n")

            f.write(str(gul[0]) + " " + str(gul[1]) + " " + str(gul[2]-15) + "\n")
            f.write(str(gur[0]) + " " + str(gur[1]) + " " + str(gur[2]-15) + "\n")
            f.write(str(glr[0]) + " " + str(glr[1]) + " " + str(glr[2]-15) + "\n")
            f.write(str(gll[0]) + " " + str(gll[1]) + " " + str(gll[2]-15) + "\n")
            f.write("numsurf 1\n")
            f.write("SURF 0x20\n")
            f.write("mat 0\n")
            f.write("refs 4\n")
            f.write("3 0 0\n")
            f.write("2 1 0\n")
            f.write("1 1 1\n")
            f.write("0 0 1\n")
            f.write("kids 0\n")

        f.close()

    def draw_epilines(self, img1, img2, lines, pts1, pts2):
        ''' img1 - image on which we draw the epilines for the points in img2
            lines - corresponding epilines '''
        r,c,d = img1.shape
        print img1.shape
        for r,pt1,pt2 in zip(lines,pts1,pts2):
            color = tuple(np.random.randint(0,255,3).tolist())
            x0,y0 = map(int, [0, -r[2]/r[1] ])
            x1,y1 = map(int, [c, -(r[2]+r[0]*c)/r[1] ])
            cv2.line(img1, (x0,y0), (x1,y1), color,1)
            cv2.circle(img1,tuple(pt1),5,color,-1)
            cv2.circle(img2,tuple(pt2),5,color,-1)
        return img1,img2

    def sfm_test(self):
        for i, i1 in enumerate(self.image_list):
            for j, pairs in enumerate(i1.match_list):
                if i == j:
                    continue
                if len(pairs) < 8:
                    # 8+ pairs are required to compute the fundamental matrix
                    continue
                i2 = self.image_list[j]
                pts1 = []
                pts2 = []
                for pair in pairs:
                    p1 = i1.kp_list[pair[0]].pt
                    p2 = i2.kp_list[pair[1]].pt
                    pts1.append( p1 )
                    pts2.append( p2 )
                pts1 = np.float32(pts1)
                pts2 = np.float32(pts2)
                print "pts1 = %s" % str(pts1)
                print "pts2 = %s" % str(pts2)
                F, mask = cv2.findFundamentalMat(pts1, pts2, cv2.FM_LMEDS)

                print "loading full res images ..."
                img1 = i1.load_full_image(self.source_dir)
                img2 = i2.load_full_image(self.source_dir)

                # Find epilines corresponding to points in right image
                # (second image) and drawing its lines on left image
                lines1 = cv2.computeCorrespondEpilines(pts2.reshape(-1,1,2), 2, F)
                lines1 = lines1.reshape(-1,3)
                img5,img6 = self.draw_epilines(img1,img2,lines1,pts1,pts2)

                # Find epilines corresponding to points in left image (first image) and
                # drawing its lines on right image
                lines2 = cv2.computeCorrespondEpilines(pts1.reshape(-1,1,2), 1,F)
                lines2 = lines2.reshape(-1,3)
                img3,img4 = self.draw_epilines(img2,img1,lines2,pts2,pts1)

                plt.subplot(121),plt.imshow(img5)
                plt.subplot(122),plt.imshow(img3)
                plt.show()

    def pnp_test(self):
        for i, i1 in enumerate(self.image_list):
            print "pnp for %s" % i1.name
            fx = (self.focal_len_mm/self.horiz_mm) * float(i1.fullw)
            fy = (self.focal_len_mm/self.vert_mm) * float(i1.fullh)
            cx = i1.fullw * 0.5
            cy = i1.fullh * 0.5
            cam = np.array( [ [fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0] ] )
            tvec_sum = [0.0, 0.0, 0.0]
            for j, pairs in enumerate(i1.match_list):
                if i == j:
                    continue
                if len(pairs) < 8:
                    # start with only well correlated pairs
                    continue
                i2 = self.image_list[j]
                img_pts = []
                obj_pts = []
                for pair in pairs:
                    p1 = i1.kp_list[pair[0]].pt
                    p2 = i2.coord_list[pair[1]]
                    img_pts.append( p1 )
                    obj_pts.append( [p2[0], p2[1], 0.0] )
                img_pts = np.float32(img_pts)
                obj_pts = np.float32(obj_pts)
                #print "img_pts = %s" % str(img_pts)
                #print "obj_pts = %s" % str(obj_pts)
                (result, rvec, tvec) = cv2.solvePnP(obj_pts, img_pts, cam, None)
                print "  result = %s, rvec = %s, tvec = %s" \
                    % (result, rvec, tvec)
                R, jac = cv2.Rodrigues(rvec)
                print "  R = %s" % str(R)
                (yaw, pitch, roll) = transformations.euler_from_matrix(R, 'rzyx')
                deg2rad = math.pi / 180.0
                yaw_deg = 180 - yaw/deg2rad
                pitch_deg = pitch/deg2rad
                print "  euler = %.2f %.2f %.2f" % (yaw/deg2rad,
                                                    pitch/deg2rad,
                                                    roll/deg2rad)
                print "  est = %.2f %.2f %.2f" % (i1.aircraft_yaw + i1.yaw_bias + self.group_yaw_bias,
                                                  i1.aircraft_pitch + i1.pitch_bias + self.group_pitch_bias,
                                                  i1.aircraft_roll + i1.roll_bias + self.group_roll_bias)