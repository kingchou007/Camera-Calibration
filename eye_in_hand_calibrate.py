import os
import cv2
import numpy as np
import rospy
from sensor_msgs.msg import Image, CameraInfo
from scipy.spatial.transform import Rotation as R
from tf.transformations import quaternion_matrix
import time
from cv_bridge import CvBridge
from flask import Flask, request, jsonify
from geometry_msgs.msg import Pose
import random
import spdlog
from std_msgs.msg import Float64MultiArray
import click
from utils import pose_to_transform

rospy.init_node('targeting', anonymous=True)

class Targeting:
    def __init__(
        self, marker_id, marker_size, ee_topic, image_topic, camera_info_topic
    ):
        self.logger = spdlog.ConsoleLogger("Targeting")
        self.logger.info("Initializing Calibration node...")

        self.acruco_id = marker_id
        self.marker_size = marker_size

        self.robot_tcp_position_sub = rospy.Subscriber(
            ee_topic, Float64MultiArray, self._read_tcp_position_sub
        )
        self.rgb_sub = rospy.Subscriber(image_topic, Image, self._bgr_callback)
        self.camera_info_sub = rospy.Subscriber(
            camera_info_topic, CameraInfo, self._camera_info_callback
        )
        self.aruco_rgb_pub = rospy.Publisher("/aruco_rgb", Image, queue_size=10)
        self.target_pose_pub = rospy.Publisher("/target_pose", Pose, queue_size=10)

        self.camera_info_loaded = False
        self._cv_bridge = CvBridge()
        self.trans_mats = []

    def _read_tcp_position_sub(self, msg):
        self.cur_tcp_pose = np.array(msg.data)

    def _g2r_callback(self):
        ret = self.cur_tcp_pose
        print("ret:", ret)
        transformation_matrix = pose_to_transform(ret[3:], mode="euler")
        print("transformation_matrix:", transformation_matrix)
        self.g2r = transformation_matrix

    def _camera_info_callback(self, msg):
        if not self.camera_info_loaded:
            self.intrinsic_matrix = {
                'fx': msg.data[0],
                'fy': msg.data[4],
                'cx': msg.data[2],
                'cy': msg.data[5]
            }
            # Optionally get distortion from somewhere if your camera is calibrated
            self.distortion_coefficients = np.zeros(5) 
            self.camera_info_loaded = True
            rospy.loginfo("Camera intrinsics loaded.")

    def _bgr_callback(self, msg):
        self.origin_image = self._cv_bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        self.bgr_image = self.origin_image.copy()

        aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_ARUCO_ORIGINAL)
        aruco_params = cv2.aruco.DetectorParameters()

        corners, ids, rejected = cv2.aruco.detectMarkers(
            self.bgr_image, aruco_dict, parameters=aruco_params
        )
        
        mtx = np.array(
            [
                [self.intrinsic_matrix["fx"], 0, self.intrinsic_matrix["cx"]],
                [0, self.intrinsic_matrix["fy"], self.intrinsic_matrix["cy"]],
                [0, 0, 1],
            ], dtype=np.float32
        )

        dist = np.zeros(5)
        rvecs, tvecs, _ = cv2.aruco.estimatePoseSingleMarkers(
            corners, self.marker_size, mtx, dist
        )

        if ids is not None:
            self.trans_mats = []
            filter_corners = []
            filter_ids = []
            for i, marker_id in enumerate(ids):
                if marker_id == self.acruco_id:
                    rvec, tvec = rvecs[i], tvecs[i]
                    R, _ = cv2.Rodrigues(rvec[0])

                    trans_mat = np.eye(4)
                    trans_mat[:3, :3] = R
                    trans_mat[:3, 3] = tvec
                    cv2.drawFrameAxes(self.bgr_image, mtx, dist, rvec, tvec, 0.05)

                    self.trans_mats.append(trans_mat)
                    filter_corners.append(corners[i])
                    filter_ids.append(ids[i])

            image_markers = cv2.aruco.drawDetectedMarkers(
                self.bgr_image.copy(), filter_corners, np.array(filter_ids)
            )
            self.aruco_rgb_pub.publish(
                self._cv_bridge.cv2_to_imgmsg(image_markers, encoding="bgr8")
            )
        else:
            self.logger.error("No AruCo markers detected.")
            self.aruco_rgb_pub.publish(
                self._cv_bridge.cv2_to_imgmsg(self.bgr_image, encoding="bgr8")
            )

    def vis_targeting(self, test=False):
        if self.trans_mats == []:
            return None

        return self.trans_mats[0]

    def calibrate(self):
        o2cs = []
        g2rs = []
        while True:
            flag = input("Press y to end calibration, or any other key to continue:")
            if flag == "y":
                break
            else:
                o2c = self.vis_targeting()
                print("o2c: ", o2c)
                if o2c is not None:
                    o2cs.append(o2c)
                    self._g2r_callback()
                    g2r = self.g2r
                    g2rs.append(np.linalg.inv(g2r))
                    self.logger.info(f"Calibration data collected. {len(o2cs)} views.")
                else:
                    self.logger.error("No AruCo markers detected.")

                if len(o2cs) >= 3:
                    R_gripper2base = [g[:3, :3] for g in g2rs]
                    t_gripper2base = [g[:3, 3] for g in g2rs]
                    R_obj2cam = [o[:3, :3] for o in o2cs]
                    t_obj2cam = [o[:3, 3] for o in o2cs]

                    R_cam2base, t_cam2base = cv2.calibrateHandEye(
                        R_gripper2base,
                        t_gripper2base,
                        R_obj2cam,
                        t_obj2cam,
                        method=cv2.CALIB_HAND_EYE_TSAI,
                    )

                    c2r = np.eye(4)
                    c2r[:3, :3] = R_cam2base
                    c2r[:3, 3] = t_cam2base[:, 0]

                    self.logger.info(
                        f"Current Calibration {len(o2cs)} views. c2r: {c2r}"
                    )

                    np.save(f"{len(o2cs)}_views_ctor.npy", c2r)
                else:
                    self.logger.warning("Not enough views collected for calibration.")


@click.command()
@click.option("--marker_id", default=582, help="Aruco Marker ID (default: 582)")
@click.option(
    "--marker_size", default=0.078, help="Aruco Marker Size in meters (default: 0.078)"
)
@click.option(
    "--ee_topic", default="/ee_pose", help="End-effector pose topic (default: /ee_pose)"
)
@click.option(
    "--image_topic",
    default="/cv_camera/image_raw",
    help="Camera image topic (default: /cv_camera/image_raw)",
)
@click.option(
    "--camera_info_topic",
    default="/robot_camera_1/intrinsics",
    help="Camera info topic (default: /cv_camera/camera_info)",
)
def main(marker_id, marker_size, ee_topic, image_topic, camera_info_topic):
    targeting = Targeting(
        marker_id=marker_id,
        marker_size=marker_size,
        ee_topic=ee_topic,
        image_topic=image_topic,
        camera_info_topic=camera_info_topic,
    )
    time.sleep(2)
    targeting.calibrate()


if __name__ == "__main__":
    main()
