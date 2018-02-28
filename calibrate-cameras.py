#!/usr/bin/python2

r'''Calibrate some synchronized cameras

Synopsis:

  $ calibrate-cameras.py --focal 2000 --imagersize 2448 2048 --outdir /tmp --object-spacing 0.015555555555555557 --object-width-n 10 '~paulo/*.png'


  ... lots of output as the solve runs ...
  done with DISTORTION_CAHVOR, optimizing DISTORTIONS again
  Wrote /tmp/camera0-0.cahvor
  Wrote /tmp/camera0-1.cahvor


This tools uses the generic mrcal platform to solve this specific common
problem. Run --help for the list of commandline options

'''

import sys
import numpy as np
import numpysane as nps
import cv2
import re
import argparse
import os
import fnmatch
import re
import subprocess
import pipes

from mrcal import cahvor
from mrcal import utils
from mrcal import poseutils
from mrcal import projections
from mrcal import cameramodel
import mrcal.optimizer as optimizer




def parse_args():
    parser = \
        argparse.ArgumentParser(description = __doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--focal',
                        type=float,
                        default=1970,
                        required=True,
                        help='Initial estimate of the focal length, in pixels')
    parser.add_argument('--imagersize',
                        nargs=2,
                        type=int,
                        default=(3904,3904),
                        help='Size of the imager. Used to extimate the coordinates of the center pixel')
    parser.add_argument('--outdir',
                        type=lambda d: d if os.path.isdir(d) else \
                                parser.error("--outdir requires an existing directory as the arg, but got '{}'".format(d)),
                        default='.',
                        help='Directory for the output camera models')
    parser.add_argument('--object-spacing',
                        required=True,
                        type=float,
                        help='Width of each square in the calibration board, in meters')
    parser.add_argument('--object-width-n',
                        type=int,
                        required=True,
                        help='How many points the calibration board has per side')
    parser.add_argument('--jobs|-j',
                        type=int,
                        default=1,
                        help='How much parallelization we want. Like GNU make')
    parser.add_argument('--dots-cache',
                        type=lambda f: f if os.path.isfile(f) else \
                                parser.error("--dots-cache requires an existing, readable file as the arg, but got '{}'".format(f)),
                        required=False,
                        help='Allows us to pass in already-computed chessboard centers')
    parser.add_argument('--muse-extrinsics',
                        required=False,
                        default=False,
                        help='''Apply MUSE's non-identity rotation for camera0''')

    parser.add_argument('images',
                        type=str,
                        nargs='+',
                        help='''A glob-per-camera for the images. Include a glob for each camera. It is
                        assumed that the image filenames in each glob are of of
                        the form xxxNNNyyy where xxx and yyy are common to all
                        images in the set, and NNN varies. This NNN is a frame
                        number, and identical frame numbers across different
                        globs signify a time-synchronized observation.''')


    return parser.parse_args()

def get_mapping_file_framecamera(files_per_camera):
    r'''Parse globs representing a sequence of images from various cameras

    I take in a list of globs (one glob per camera) where each glob represents
    all the images from that camera. I return a dict that maps each image
    filename to (framenumber,cameraindex)

    '''

    def get_longest_leading_trailing_substrings(strings):
        r'''Given a list of strings, returns the length of the longest leading and
        trailing substring common to all the strings

        Main use case is to take in strings such as

          a/b/c/frame001.png
          a/b/c/frame002.png
          a/b/c/frame003.png

        and return ("a/b/c/frame00", ".png")

        '''

        # These feel inefficient, especially being written in python. There's
        # probably some built-in primitive I'm not seeing
        def longest_leading_substring(a,b):
            for i in xrange(len(a)):
                if i >= len(b) or a[i] != b[i]:
                    return a[:i]
            return a
        def longest_trailing_substring(a,b):
            for i in xrange(len(a)):
                if i >= len(b) or a[-i-1] != b[-i-1]:
                    if i == 0:
                        return ''
                    return a[-i:]
            return a

        if not strings:
            return (None,None)

        leading  = strings[0]
        trailing = strings[0]

        for s in strings[1:]:
            leading  = longest_leading_substring (leading,s)
            trailing = longest_trailing_substring(trailing,s)
        return leading,trailing

    def pull_framenumbers(files):

        leading,trailing = get_longest_leading_trailing_substrings(files)
        Nleading  = len(leading)
        Ntrailing = len(trailing)

        # I now have leading and trailing substrings. I make sure that all the stuff
        # between the leading and trailing strings is numeric

        # needed because I want s[i:-0] to mean s[i:], but that doesn't work, but
        # s[i:None] does
        Itrailing = -Ntrailing if Ntrailing > 0 else None
        for f in files:
            if not re.match("^[0-9]+$", f[Nleading:Itrailing]):
                raise Exception(("Image globs MUST be of the form 'something..number..something\n" +   \
                                 "where the somethings are common to all the filenames. File '{}'\n" + \
                                 "has a non-numeric middle: '{}'. The somethings are: '{}' and '{}\n" + \
                                 "Did you forget to pass globs for each camera separately?"). \
                                format(f, f[Nleading:Itrailing],
                                       leading, trailing))

        # Alrighty. The centers are all numeric. I gather all the digits around the
        # centers, and I'm done
        m = re.match("^.*?([0-9]*)$", leading)
        if m:
            pre_numeric = m.group(1)
        else:
            pre_numeric = ''
        m = re.match("^([0-9]*).*?$", trailing)
        if m:
            post_numeric = m.group(1)
        else:
            post_numeric = ''

        return [int(pre_numeric + f[Nleading:Itrailing] + post_numeric) for f in files]




    Ncameras = len(files_per_camera)
    mapping = {}
    for icamera in xrange(Ncameras):
        framenumbers = pull_framenumbers(files_per_camera[icamera])
        mapping.update(zip(files_per_camera[icamera], [(iframe,icamera) for iframe in framenumbers]))
    return mapping

def get_observations(Nw, Nh, globs, dots_vnl=None):
    r'''Computes the point observations and returns them in a usable form

    We are given globs of images (one glob per camera), where the filenames
    encode the instantaneous frame numbers. This function invokes the chessboard
    finder to compute the point coordinates, and returns a tuple

      observations, indices_frame_camera, files_sorted

    where observations is an (N,object-width-n,object-width-n,2) array
    describing N board observations where the board has dimensions
    (object-width-n,object-width-n) and each point is an (x,y) pixel observation

    indices_frame_camera is an (N,2) array of integers where each observation is
    (index_frame,index_camera)

    files_sorted is a list of paths of images corresponding to the observations

    '''

    def get_dot_observations(Nw, Nh, globs, dots_vnl=None):
        r'''Invokes mrgingham to get dot observations

        Returns a dict mapping from filename to a numpy array with a full grid
        of dot observations. If no grid was observed in a particular image, the
        relevant dict entry is empty

        This function takes an optional dots_vnl argument. If given, this is
        cached chessboard-finder results. The globs are then used to match image
        paths in this file, NOT on disk. If dots_vnl. is not None then we don't
        even need to have the original images

        '''

        Ncameras = len(globs)
        files_per_camera = []
        for i in xrange(Ncameras):
            files_per_camera.append([])

        def accum_files(f):
            for i_camera in xrange(Ncameras):
                if fnmatch.fnmatch(f, globs[i_camera]):
                    files_per_camera[i_camera].append(f)
                    return
            raise Exception("File '{}' didn't match any of the globs '{}'".format(f,globs))



        if dots_vnl is None:
            if Nw != 10 or Nh != 10:
                raise Exception("mrgingham currently accepts ONLY 10x10 grids")

            args_mrgingham = ['mrgingham_from_image', '--chessboard', '--blur', '3', '--clahe', '--jobs', '10']
            args_mrgingham.extend(globs)

            sys.stderr.write("Computing chessboard corners from {}\n".format(globs))
            sys.stderr.write("Command:  {}\n".format(' '.join(pipes.quote(s) for s in args_mrgingham)))

            dots_output = subprocess.Popen(args_mrgingham, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            pipe = dots_output.stdout
        else:
            pipe = open(dots_vnl, 'r')


        mapping = {}
        context = {'f':    '',
                   'grid': np.array(())}

        def finish():
            if context['grid'].size:
                if Nw*Nh != context['grid'].size/2:
                    raise Exception("File '{}' expected to have {}*{}={} elements, but got {}". \
                                    format(context['f'], Nw,Nh,Nw*Nh, context['grid'].size/2))
                accum_files(context['f'])
                mapping[context['f']] = context['grid']
                context['f']    = ''
                context['grid'] = np.array(())

        for line in pipe:
            if line[0] == '#':
                continue
            m = re.match('(\S+)\s+(.*?)$', line)
            if m is None:
                raise Exception("Unexpected line in the dots output: '{}'".format(line))
            if m.group(2)[:2] == '- ':
                finish()
                continue
            if context['f'] != m.group(1):
                finish()
                context['f'] = m.group(1)

            context['grid'] = nps.glue(context['grid'],
                                       np.fromstring(m.group(2), sep=' ', dtype=np.float),
                                       axis=-2)
        finish()

        if dots_vnl is None:
            sys.stderr.write("Done computing chessboard corners from {}\n".format(globs))

            if dots_output.wait() != 0:
                err = dots_output.stderr.read()
                raise Exception("mrgingham_from_image failed: {}".format(err))
        else:
            pipe.close()
        return mapping,files_per_camera


    indices_frame_camera = np.array((), dtype=np.int32)
    observations         = np.array((), dtype=float)

    # basic logic is this:
    #   for frames:
    #       for cameras:
    #           if have observation:
    #               push observations
    #               push indices_frame_camera

    # inputs[camera][image] = (image_filename, frame_number)
    mapping_file_dots,files_per_camera = get_dot_observations(Nw, Nh, globs, dots_vnl)
    mapping_file_framecamera           = get_mapping_file_framecamera(files_per_camera)

    # I create a file list sorted by frame and then camera. So my for(frames)
    # {for(cameras) {}} loop will just end up looking at these files in order
    files_sorted = sorted(mapping_file_dots.keys(), key=lambda f: mapping_file_framecamera[f][1])
    files_sorted = sorted(files_sorted,             key=lambda f: mapping_file_framecamera[f][0])

    i_observation = 0

    i_frame_last = None
    index_frame  = -1
    for f in files_sorted:
        # The frame indices I return are consecutive starting from 0, NOT the
        # original frame numbers
        i_frame,i_camera = mapping_file_framecamera[f]
        if i_frame_last == None or i_frame_last != i_frame:
            index_frame += 1
            i_frame_last = i_frame

        indices_frame_camera = nps.glue(indices_frame_camera,
                                        np.array((index_frame, i_camera), dtype=np.int32),
                                        axis=-2)
        observations = nps.glue(observations,
                                mapping_file_dots[f].reshape(Nh,Nw,2),
                                axis=-4)

    return observations, indices_frame_camera, files_sorted


def estimate_local_calobject_poses( indices_frame_camera, \
                                    dots, dot_spacing, focal, imagersize,
                                    Nwant):
    r"""Estimates pose of observed object in a single-camera view

    Given observations, and an estimate of camera intrinsics (focal lengths,
    imager size) computes an estimate of the pose of the calibration object in
    respect to the camera for each frame. This assumes that all frames are
    independent and all cameras are independent. This assumes a pinhole camera.

    This function is a wrapper around the solvePnP() openCV call, which does all
    the work.

    The observations are given in a numpy array with axes:

      (iframe, idot_x, idot_y, idot2d_xy)

    So as an example, the observed pixel coord of the dot (3,4) in frame index 5
    is the 2-vector dots[5,3,4,:]

    Missing observations are given as negative pixel coords.

    This function returns an (Nobservations,4,3) array, with the observations
    aligned with the dots and indices_frame_camera arrays. Each observation
    slice is (4,3) in glue(R, t, axis=-2)

    """

    Nobservations = indices_frame_camera.shape[0]

    # this wastes memory, but makes it easier to keep track of which data goes
    # with what
    Rt_all = np.zeros( (Nobservations, 4, 3), dtype=float)
    camera_matrix = np.array((( focal, 0,        (imagersize[0] - 1)/2), \
                              (        0, focal, (imagersize[1] - 1)/2), \
                              (        0,        0,                 1)))

    full_object = utils.get_full_object(Nwant, Nwant, dot_spacing)

    for i_observation in xrange(dots.shape[0]):
        d = dots[i_observation, ...]

        d = nps.clump( nps.glue(d, full_object, axis=-1), n=2)
        # d is (Nwant*Nwant,5); each row is an xy pixel observation followed by the xyz
        # coord of the point in the calibration object. I pick off those rows
        # where the observations are both >= 0. Result should be (N,5) where N
        # <= Nwant*Nwant
        i = (d[..., 0] >= 0) * (d[..., 1] >= 0)
        d = d[i,:]

        observations = d[:,:2]
        ref_object   = d[:,2:]
        result,rvec,tvec = cv2.solvePnP(ref_object  [..., np.newaxis],
                                        observations[..., np.newaxis],
                                        camera_matrix, None)
        if not result:
            raise Exception("solvePnP failed!")
        if tvec[2] <= 0:
            raise Exception("solvePnP says that tvec.z <= 0. Maybe needs a flip, but please examine this")

        Rt_all[i_observation, :, :] = poseutils.Rt_from_rt(nps.glue(rvec.ravel(), tvec.ravel(), axis=-1))


    return Rt_all

def estimate_camera_poses( calobject_poses_Rt, indices_frame_camera, \
                           dots, dot_spacing, Ncameras,
                           Nwant):
    r'''Estimate camera poses in respect to each other

    We are given poses of the calibration object in respect to each observing
    camera. We also have multiple cameras observing the same calibration object
    at the same time, and we have local poses for each. We can thus compute the
    relative camera pose from these observations.

    We have many frames that have different observations from the same set of
    fixed-relative-pose cameras, so we compute the relative camera pose to
    optimize the observations

    '''

    # This is a bit of a hack. I look at the correspondence of camera0 to camera
    # i for i in 1:N-1. I ignore all correspondences between cameras i,j if i!=0
    # and j!=0. Good enough for now
    full_object = utils.get_full_object(Nwant, Nwant, dot_spacing)
    Rt = np.array(())


    for i_camera in xrange(1,Ncameras):
        A = np.array(())
        B = np.array(())

        # I traverse my observation list, and pick out observations from frames
        # that had data from both camera 0 and camera i
        i_frame_last = -1
        d0  = None
        d1  = None
        Rt0 = None
        Rt1 = None
        for i_observation in xrange(dots.shape[0]):
            i_frame_this,i_camera_this = indices_frame_camera[i_observation, ...]
            if i_frame_this != i_frame_last:
                d0  = None
                d1  = None
                Rt0 = None
                Rt1 = None
                i_frame_last = i_frame_this

            if i_camera_this == 0:
                if Rt0 is not None:
                    raise Exception("Saw multiple camera0 observations in frame {}".format(i_frame_this))
                Rt0 = calobject_poses_Rt[i_observation, ...]
                d0  = dots[i_observation, ...]
            if i_camera_this == i_camera:
                if Rt1 is not None:
                    raise Exception("Saw multiple camera{} observations in frame {}".format(i_camera_this,
                                                                                            i_frame_this))
                Rt1 = calobject_poses_Rt[i_observation, ...]
                d1  = dots[i_observation, ...]

                if Rt0 is None: # have camera1 observation, but not camera0
                    continue


                # d looks at one frame and has shape (Nwant,Nwant,7). Each row is
                #   xy pixel observation in left camera
                #   xy pixel observation in right camera
                #   xyz coord of dot in the calibration object coord system
                d = nps.glue( d0, d1, full_object, axis=-1 )

                # squash dims so that d is (Nwant*Nwant,7)
                d = nps.clump(d, n=2)

                ref_object = nps.clump(full_object, n=2)

                # # It's possible that I could have incomplete views of the
                # # calibration object, so I pull out only those point
                # # observations that have a complete view. In reality, I
                # # currently don't accept any incomplete views, and much outside
                # # code would need an update to support that. This doesn't hurt, however

                # # d looks at one frame and has shape (10,10,7). Each row is
                # #   xy pixel observation in left camera
                # #   xy pixel observation in right camera
                # #   xyz coord of dot in the calibration object coord system
                # d = nps.glue( d0, d1, full_object, axis=-1 )

                # # squash dims so that d is (100,7)
                # d = nps.transpose(nps.clump(nps.mv(d, -1, -3), n=2))

                # # I pick out those points that have observations in both frames
                # i = (d[..., 0] >= 0) * (d[..., 1] >= 0) * (d[..., 2] >= 0) * (d[..., 3] >= 0)
                # d = d[i,:]

                # # ref_object is (N,3)
                # ref_object = d[:,4:]

                A = nps.glue(A, nps.matmult( ref_object, nps.transpose(Rt0[:3,:])) + Rt0[3,:],
                             axis = -2)
                B = nps.glue(B, nps.matmult( ref_object, nps.transpose(Rt1[:3,:])) + Rt1[3,:],
                             axis = -2)

        Rt = nps.glue(Rt, utils.align3d_procrustes(A, B),
                      axis=-3)

    return Rt

def estimate_frame_poses(calobject_poses_Rt, camera_poses_Rt, indices_frame_camera, dot_spacing,
                         Nwant):
    r'''Estimate poses of the calibration object observations

    We're given

    calobject_poses_Rt:

      an array of dimensions (Nobservations,4,3) that contains a
      calobject-to-camera transformation estimate, for each observation of the
      board

    camera_poses_Rt:

      an array of dimensions (Ncameras-1,4,3) that contains a camerai-to-camera0
      transformation estimate. camera0-to-camera0 is the identity, so this isn't
      stored

    indices_frame_camera:

      an array of shape (Nobservations,2) that indicates which frame and which
      camera has observed the board

    With this data, I return an array of shape (Nframes,6) that contains an
    estimate of the pose of each frame, in the camera0 coord system. Each row is
    (r,t) where r is a Rodrigues rotation and t is a translation that map points
    in the calobject coord system to that of camera 0

    '''


    def process(i_observation0, i_observation1):
        R'''Given a range of observations corresponding to the same frame, estimate the
        frame pose'''

        def T_camera_board(i_observation):
            r'''Transform from the board coords to the camera coords'''
            i_frame,i_camera = indices_frame_camera[i_observation, ...]

            Rt_f = calobject_poses_Rt[i_observation, :,:]
            if i_camera == 0:
                return Rt_f

            # T_cami_cam0 T_cam0_board = T_cami_board
            Rt_cam = camera_poses_Rt[i_camera-1, ...]

            return poseutils.compose_Rt( Rt_cam, Rt_f)


        # frame poses should map FROM the frame coord system TO the ref coord
        # system (camera 0).

        # special case: if there's a single observation, I just use it
        if i_observation1 - i_observation0 == 1:
            return T_camera_board(i_observation0)

        # Multiple cameras have observed the object for this frame. I have an
        # estimate of these for each camera. I merge them in a lame way: I
        # average out the positions of each point, and fit the calibration
        # object into the mean point cloud
        obj = utils.get_full_object(Nwant, Nwant, dot_spacing)

        sum_obj_unproj = obj*0
        for i_observation in xrange(i_observation0, i_observation1):
            Rt = T_camera_board(i_observation)
            sum_obj_unproj += poseutils.transform_point_Rt(Rt, obj)

        mean = sum_obj_unproj / (i_observation1 - i_observation0)

        # Got my point cloud. fit

        # transform both to shape = (N*N, 3)
        obj  = nps.clump(obj,  n=2)
        mean = nps.clump(mean, n=2)
        return utils.align3d_procrustes( mean, obj )





    frame_poses_rt = np.array(())

    i_frame_current          = -1
    i_observation_framestart = -1;

    for i_observation in xrange(indices_frame_camera.shape[0]):
        i_frame,i_camera = indices_frame_camera[i_observation, ...]

        if i_frame != i_frame_current:
            if i_observation_framestart >= 0:
                Rt = process(i_observation_framestart, i_observation)
                frame_poses_rt = nps.glue(frame_poses_rt, poseutils.rt_from_Rt(Rt), axis=-2)

            i_observation_framestart = i_observation
            i_frame_current = i_frame

    if i_observation_framestart >= 0:
        Rt = process(i_observation_framestart, indices_frame_camera.shape[0])
        frame_poses_rt = nps.glue(frame_poses_rt, poseutils.rt_from_Rt(Rt), axis=-2)

    return frame_poses_rt

def make_seed(inputs):
    r'''Generate a solution seed for a given input'''


    def make_intrinsics_vector(i_camera, inputs):
        imager_w,imager_h = inputs['imagersize']
        return np.array( (inputs['focal_estimate'], inputs['focal_estimate'],
                          float(imager_w-1)/2.,
                          float(imager_h-1)/2.))




    intrinsics = nps.cat( *[make_intrinsics_vector(i_camera, inputs) \
                            for i_camera in xrange(inputs['Ncameras'])] )

    # I compute an estimate of the poses of the calibration object in the local
    # coord system of each camera for each frame. This is done for each frame
    # and for each camera separately. This isn't meant to be precise, and is
    # only used for seeding.
    #
    # I get rotation, translation in a (4,3) array, such that R*calobject + t
    # produces the calibration object points in the coord system of the camera.
    # The result has dimensions (N,4,3)
    calobject_poses_Rt = \
        estimate_local_calobject_poses( inputs['indices_frame_camera'],
                                        inputs['dots'],
                                        inputs['dot_spacing'],
                                        inputs['focal_estimate'],
                                        inputs['imagersize'],
                                        inputs['object_width_n'])
    # these map FROM the coord system of the calibration object TO the coord
    # system of this camera

    # I now have a rough estimate of calobject poses in the coord system of each
    # frame. One can think of these as two sets of point clouds, each attached to
    # their camera. I can move around the two sets of point clouds to try to match
    # them up, and this will give me an estimate of the relative pose of the two
    # cameras in respect to each other. I need to set up the correspondences, and
    # align3d_procrustes() does the rest
    #
    # I get transformations that map points in 1-Nth camera coord system to 0th
    # camera coord system. Rt have dimensions (N-1,4,3)
    camera_poses_Rt = estimate_camera_poses( calobject_poses_Rt,
                                             inputs['indices_frame_camera'],
                                             inputs['dots'],
                                             inputs['dot_spacing'],
                                             inputs['Ncameras'],
                                             inputs['object_width_n'])

    if len(camera_poses_Rt):
        # extrinsics should map FROM the ref coord system TO the coord system of the
        # camera in question. This is backwards from what I have
        extrinsics = nps.atleast_dims( poseutils.rt_from_Rt(poseutils.invert_Rt(camera_poses_Rt)),
                                       -2 )
    else:
        extrinsics = np.zeros((0,6))

    frames = \
        estimate_frame_poses(calobject_poses_Rt, camera_poses_Rt,
                             inputs['indices_frame_camera'],
                             inputs['dot_spacing'],
                             inputs['object_width_n'])
    return intrinsics,extrinsics,frames









args = parse_args()
if len(args.images) > 10:
    N = len(args.images)
    raise Exception("Got {} image globs. It should be one glob per camera, and this sounds like WAY too make cameras. Did you forget to escape your glob?". \
                    format(N))

images         = [os.path.expanduser(g) for g in args.images]
object_spacing = args.object_spacing
object_width_n = args.object_width_n


Ncameras = len(images)

observations, indices_frame_camera,paths = \
    get_observations(object_width_n,
                     object_width_n,
                     images,
                     args.dots_cache)

inputs = {'imagersize':           args.imagersize,
          'focal_estimate':       args.focal,
          'Ncameras':             Ncameras,
          'indices_frame_camera': indices_frame_camera,
          'dots':                 observations,
          'dot_spacing':          object_spacing,
          'object_width_n':       object_width_n}



intrinsics,extrinsics,frames = make_seed(inputs)

# done with everything. Run the calibration, in several passes.

distortion_model = "DISTORTION_NONE"
optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = False,
                   do_optimize_intrinsic_distortions = False,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)

distortion_model = "DISTORTION_NONE"
optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = True,
                   do_optimize_intrinsic_distortions = False,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)

print "done with {}".format(distortion_model)

Ndistortions0      = optimizer.getNdistortionParams(distortion_model)

distortion_model   = "DISTORTION_CAHVOR"
Ndistortions       = optimizer.getNdistortionParams(distortion_model)
Ndistortions_delta = Ndistortions - Ndistortions0
intrinsics         = nps.glue( intrinsics, np.random.random((Ncameras, Ndistortions_delta))*1e-5, axis=-1 )
optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = False,
                   do_optimize_intrinsic_distortions = True,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing DISTORTIONS".format(distortion_model)

optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = True,
                   do_optimize_intrinsic_distortions = False,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing CORE".format(distortion_model)


optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = False,
                   do_optimize_intrinsic_distortions = True,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing DISTORTIONS again".format(distortion_model)

optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = True,
                   do_optimize_intrinsic_distortions = False,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing CORE".format(distortion_model)


optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = False,
                   do_optimize_intrinsic_distortions = True,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing DISTORTIONS again".format(distortion_model)

optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = True,
                   do_optimize_intrinsic_distortions = False,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing CORE".format(distortion_model)


optimizer.optimize(intrinsics, extrinsics, frames, None,
                   observations, indices_frame_camera,
                   None, None,
                   distortion_model,

                   do_optimize_intrinsic_core        = False,
                   do_optimize_intrinsic_distortions = True,
                   calibration_object_spacing        = object_spacing,
                   calibration_object_width_n        = object_width_n)
print "done with {}, optimizing DISTORTIONS again".format(distortion_model)



for i_camera in xrange(Ncameras):
    if args.muse_extrinsics:
        Rt_r0 = np.array([[ 0.,  0.,  1.],
                          [ 1.,  0.,  0.],
                          [ 0.,  1.,  0.],
                          [ 0.,  0.,  0.]])
    else:
        Rt_r0 = np.array([[ 1.,  0.,  0.],
                          [ 0.,  1.,  0.],
                          [ 0.,  0.,  1.],
                          [ 0.,  0.,  0.]])

    if i_camera >= 1:
        rt_x0 = extrinsics[i_camera-1,:].ravel()
    else:
        rt_x0 = np.zeros(6)
    Rt_rx = poseutils.compose_Rt(Rt_r0,
                                 poseutils.invert_Rt( poseutils.Rt_from_rt(rt_x0)))

    c = cameramodel( intrinsics          = (distortion_model, intrinsics[i_camera,:]),
                     extrinsics_Rt_toref = Rt_rx )

    with open('{}/camera-{}.cahvor'.format(args.outdir, i_camera), 'w') as f:
        print "Wrote {}".format(f.name)
        f.write("## generated with {}\n\n".format(sys.argv))
        cahvor.write(f, c)


projected = projections.calobservations_project(distortion_model, intrinsics, extrinsics, frames, object_spacing, object_width_n)
err       = projections.calobservations_compute_reproj_error(projected, observations,
                                                             indices_frame_camera, object_width_n)
norm2_err_perimage = nps.inner( nps.clump(err,n=-3),
                                nps.clump(err,n=-3) )
rms_err_perimage   = np.sqrt( norm2_err_perimage / (object_width_n*object_width_n) )

i_observations_worst = list(reversed(np.argsort(rms_err_perimage)))
print "worst observations: {}".format(i_observations_worst[:100])
print "worst frame_camera indices and RMS:\n{}".format(nps.glue( indices_frame_camera[i_observations_worst,:],
                                                                nps.transpose(rms_err_perimage[i_observations_worst]), axis = -1))
print "worst image paths: {}".format([paths[p] for p in i_observations_worst])






i_observation = i_observations_worst[0]

obs = nps.clump( observations[i_observation], n=2)
i_frame,i_camera = indices_frame_camera[i_observation]
reproj = nps.clump( projected[i_frame,i_camera], n=2)

# error per dot
err = np.sqrt(nps.inner(reproj - obs,
                        reproj - obs))

import gnuplotlib as gp
gp.plot( (reproj[:,0], reproj[:,1], err,
          {'with': 'points pt 7 ps 2 palette', 'legend': 'reprojection error', 'tuplesize': 3}),
         (obs   [:,0], obs   [:,1], {'with': 'points', 'legend': 'observed'}),
         (reproj[:,0], reproj[:,1], {'with': 'points', 'legend': 'hypothesis'}),
         rgbimage=paths[i_observation],
         square=1,cbmin=0,
         _set='autoscale noextend',
         title='Worst case. i_frame={}, i_observation={}, i_camera={}, path={}'.format( i_frame, i_observation, i_camera, paths[i_observation]))
import time
time.sleep(10000)

