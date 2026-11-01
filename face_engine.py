import face_recognition
import numpy as np
import cv2


# ============================================================
# FACE ENCODING
# ============================================================

def get_face_encoding(image_path):
    """
    Generate a 128-dimensional face encoding.

    Multiple detection methods and image preprocessing
    techniques are tried to make webcam registration
    more reliable.

    Returns:
        list: 128-dimensional face encoding
        None: if exactly one face cannot be detected
    """

    try:

        # ----------------------------------------------------
        # Load image
        # ----------------------------------------------------

        image = cv2.imread(image_path)

        if image is None:
            print("ERROR: Could not read image.")
            return None

        print("=" * 60)
        print("FACE ENGINE")
        print("=" * 60)

        print("Image path:", image_path)
        print("Image shape:", image.shape)

        # ----------------------------------------------------
        # Convert OpenCV BGR → RGB
        # ----------------------------------------------------

        rgb_original = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2RGB
        )

        # ----------------------------------------------------
        # Create different versions of the image
        # ----------------------------------------------------

        images_to_try = []

        # 1. Original image
        images_to_try.append(
            ("Original", rgb_original)
        )

        # ----------------------------------------------------
        # 2. Brightness enhanced image
        # ----------------------------------------------------

        lab = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2LAB
        )

        l_channel, a_channel, b_channel = cv2.split(lab)

        clahe = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8)
        )

        l_channel = clahe.apply(l_channel)

        enhanced_lab = cv2.merge(
            (l_channel, a_channel, b_channel)
        )

        enhanced_bgr = cv2.cvtColor(
            enhanced_lab,
            cv2.COLOR_LAB2BGR
        )

        enhanced_rgb = cv2.cvtColor(
            enhanced_bgr,
            cv2.COLOR_BGR2RGB
        )

        images_to_try.append(
            ("Brightness Enhanced", enhanced_rgb)
        )

        # ----------------------------------------------------
        # 3. Brightened image
        # ----------------------------------------------------

        brightened = cv2.convertScaleAbs(
            image,
            alpha=1.15,
            beta=25
        )

        brightened_rgb = cv2.cvtColor(
            brightened,
            cv2.COLOR_BGR2RGB
        )

        images_to_try.append(
            ("Brightened", brightened_rgb)
        )

        # ----------------------------------------------------
        # 4. Upscaled image
        # ----------------------------------------------------

        height, width = image.shape[:2]

        scale = 1.5

        upscaled = cv2.resize(
            image,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC
        )

        upscaled_rgb = cv2.cvtColor(
            upscaled,
            cv2.COLOR_BGR2RGB
        )

        images_to_try.append(
            ("Upscaled", upscaled_rgb)
        )

        # ----------------------------------------------------
        # Try face_recognition HOG detector
        # ----------------------------------------------------

        for image_name, rgb_image in images_to_try:

            print(
                f"Trying face detection: {image_name}"
            )

            # First attempt
            locations = face_recognition.face_locations(
                rgb_image,
                model="hog",
                number_of_times_to_upsample=1
            )

            print(
                f"Faces detected: {len(locations)}"
            )

            # Second attempt with more detailed scanning
            if len(locations) == 0:

                locations = face_recognition.face_locations(
                    rgb_image,
                    model="hog",
                    number_of_times_to_upsample=2
                )

                print(
                    f"Faces detected after second scan: "
                    f"{len(locations)}"
                )

            # Exactly one face
            if len(locations) == 1:

                print(
                    f"SUCCESS: One face detected using "
                    f"{image_name}"
                )

                encodings = face_recognition.face_encodings(
                    rgb_image,
                    known_face_locations=locations,
                    num_jitters=2
                )

                if len(encodings) == 1:

                    print(
                        "SUCCESS: Face encoding generated."
                    )

                    print("=" * 60)

                    return encodings[0].tolist()

        # ----------------------------------------------------
        # OpenCV Haar Cascade fallback
        # ----------------------------------------------------

        print(
            "face_recognition detector did not find "
            "a valid face."
        )

        print(
            "Trying OpenCV Haar Cascade fallback..."
        )

        gray = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2GRAY
        )

        # Improve contrast
        gray = cv2.equalizeHist(gray)

        cascade_path = (
            cv2.data.haarcascades
            + "haarcascade_frontalface_default.xml"
        )

        detector = cv2.CascadeClassifier(
            cascade_path
        )

        faces = detector.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(80, 80)
        )

        print(
            f"OpenCV Haar faces detected: {len(faces)}"
        )

        # Exactly one face
        if len(faces) == 1:

            x, y, w, h = faces[0]

            print(
                "OpenCV detected exactly one face."
            )

            # Convert OpenCV coordinates:
            # (x, y, width, height)
            #
            # face_recognition coordinates:
            # (top, right, bottom, left)

            top = y
            right = x + w
            bottom = y + h
            left = x

            # Add a small margin around the face
            margin_x = int(w * 0.15)
            margin_y = int(h * 0.20)

            top = max(0, top - margin_y)
            right = min(rgb_original.shape[1], right + margin_x)
            bottom = min(rgb_original.shape[0], bottom + margin_y)
            left = max(0, left - margin_x)

            location = (
                top,
                right,
                bottom,
                left
            )

            encodings = face_recognition.face_encodings(
                rgb_original,
                known_face_locations=[location],
                num_jitters=2
            )

            if len(encodings) == 1:

                print(
                    "SUCCESS: Encoding generated "
                    "using OpenCV fallback."
                )

                print("=" * 60)

                return encodings[0].tolist()

        elif len(faces) > 1:

            print(
                "Multiple faces detected by OpenCV."
            )

            print("=" * 60)

            return None

        # ----------------------------------------------------
        # Nothing worked
        # ----------------------------------------------------

        print("=" * 60)
        print(
            "FAILED: No valid single face found."
        )
        print("=" * 60)

        return None

    except Exception as error:

        print("=" * 60)
        print(
            "FACE ENCODING ERROR:",
            error
        )
        print("=" * 60)

        return None


# ============================================================
# FACE COMPARISON
# ============================================================

def compare_faces(
    known_encoding,
    face_encoding,
    tolerance=0.50
):
    """
    Compare stored face encoding with
    a newly detected face.

    Returns:
        match: True / False
        confidence: 0.0 - 1.0
    """

    try:

        known = np.array(
            known_encoding,
            dtype=np.float64
        )

        unknown = np.array(
            face_encoding,
            dtype=np.float64
        )

        # ----------------------------------------------------
        # Validate encoding sizes
        # ----------------------------------------------------

        if known.shape != (128,):

            print(
                "Invalid known encoding:",
                known.shape
            )

            return False, 0.0

        if unknown.shape != (128,):

            print(
                "Invalid unknown encoding:",
                unknown.shape
            )

            return False, 0.0

        # ----------------------------------------------------
        # Calculate distance
        # ----------------------------------------------------

        distance = face_recognition.face_distance(
            [known],
            unknown
        )[0]

        # ----------------------------------------------------
        # Match
        # ----------------------------------------------------

        match = distance <= tolerance

        # ----------------------------------------------------
        # Confidence
        # ----------------------------------------------------

        confidence = max(
            0.0,
            min(
                1.0,
                1.0 - float(distance)
            )
        )

        print(
            f"Face distance: {distance:.4f}"
        )

        print(
            f"Face match: {match}"
        )

        print(
            f"Confidence: {confidence:.2%}"
        )

        return match, confidence

    except Exception as error:

        print(
            f"Face comparison error: {error}"
        )

        return False, 0.0